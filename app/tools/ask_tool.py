"""Ask Ekavo as tools, so an assistant (Claude, ChatGPT) or the command line can hand a longer job to Ekavo's own planner and keep a conversation going.

Three tools: `ask.say` (say what you want; a plan comes back with its price, or a plain answer), `ask.confirm` (the person's yes to the price shown, which runs it) and
`ask.status` (where a request stands). They are the same code as the public ask routes (`/api/public/v1/ask`) and the in-app Ask desk, so the same planner, prices,
plan limits and approvals apply, and nothing here can publish: a request that would publish is shown to the person and never run.

* Only a person's own sign-in (a command line or an assistant) may use them. A service key may not: Ask spends the member's credits outside any key's cap, so a key
  would be a way round its cap.
* Ask does not offer these tools to its own planner (`runs._HIDDEN`), so it can never call itself.
* A step Ask runs is recorded as its own change with its own undo (see `undo.py`); these tools only plan and start."""
from __future__ import annotations

from typing import Any, Optional

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from app.tools.contract import Mode, Risk, Scope, ToolSpec
from app.tools.executor import ToolFailure, handler
from app.tools.gatekeeper import Admission


class SayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000, description="What you want, in your own words and language.")
    thread_id: Optional[str] = Field(None, max_length=64, description="Send the thread_id from an earlier answer to carry on in the same conversation.")


class RunRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)


class AskOut(BaseModel):
    model_config = ConfigDict(extra="allow")


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="ask.say", summary="Say what you want in plain words. Ekavo plans it and shows the price, or just answers. Keep the thread_id to carry on the conversation.",
            risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=SayIn, output_model=AskOut, permission="create_content",
            cost_note="A plain answer is free. A plan shows its price first and nothing runs until ask.confirm.", source="query:ask.say",
            examples=("Write three posts about our launch and make a picture for each", "Make that shorter"), version=1,
        ),
        ToolSpec(
            name="ask.confirm", summary="Say yes to the price Ekavo showed for a request, so it runs. Nothing runs, and nothing is charged, before this.",
            risk=Risk.WRITE, scope=Scope.CREATE, mode=Mode.INSTANT, input_model=RunRef, output_model=AskOut, permission="create_content",
            cost_note="The price shown for that request. The steps are charged as they run and given back if they fail.", source="query:ask.confirm", examples=("Yes, go ahead",), version=1,
        ),
        ToolSpec(
            name="ask.status", summary="Show where a request made with ask.say stands: its plan, each step and the result.", risk=Risk.READ, scope=Scope.READ, mode=Mode.INSTANT,
            input_model=RunRef, output_model=AskOut, source="query:ask.status", examples=("How did that go?",), version=1,
        ),
    ]


def _person(admission: Admission) -> tuple[str, str, str]:
    """(workspace, member, place) for a person's own sign-in; refuses a service key."""
    cred = admission.credential
    if cred.surface not in ("cli", "mcp") or not cred.user_id:
        raise ToolFailure("not_allowed", "Ask works from a signed-in command line or assistant, not a service key.", 403)
    return cred.workspace_id, cred.user_id, cred.surface


@handler("ask.say")
async def say_handler(admission: Admission) -> dict[str, Any]:
    from app.ask import runs, threads

    workspace_id, user_id, surface = _person(admission)
    args = admission.arguments
    thread_id = args.thread_id or (await threads.create(workspace_id, user_id, surface, title=args.message))["id"]
    run = await runs.create(workspace_id, user_id, args.message, "ask", thread_id=thread_id, surface=surface)
    return jsonable_encoder({"thread_id": thread_id, "run": run})


@handler("ask.confirm")
async def confirm_handler(admission: Admission) -> dict[str, Any]:
    from app.ask import runs

    workspace_id, user_id, _surface = _person(admission)
    return jsonable_encoder({"run": await runs.confirm(workspace_id, user_id, admission.arguments.run_id)})


@handler("ask.status")
async def status_handler(admission: Admission) -> dict[str, Any]:
    from app.ask import runs

    workspace_id, user_id, _surface = _person(admission)
    return jsonable_encoder({"run": await runs.get(workspace_id, user_id, admission.arguments.run_id)})
