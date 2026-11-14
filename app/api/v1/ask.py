"""Ask Ekavo, as the signed-in member sees it: say what you want, see the plan and its price, say yes, watch it work, stop or take it back.

Every step goes through the same gatekeeper as the public API, with the member's own role, and shows in the same record of changes (tagged as made by
Ask), so each one can be undone from Settings, Connections. Ask can read and create; publishing, approving, deleting and anything that needs the
person's own yes is shown to them and never done for them."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.ask import runs, threads
from app.channels import engine as channel_engine
from app.pipelines.media import request_refs
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace
from app.tools.executor import ToolFailure

router = APIRouter()


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=runs.MAX_MESSAGE)
    trust: Literal["suggest", "ask", "act"] = "ask"
    # the conversation this belongs to; left out, a new conversation is started and its id comes back as `thread_id`
    thread_id: Optional[str] = Field(None, max_length=64)
    # pictures sent with this request (from POST /references); their look guides any pictures made for it
    reference_ids: list[str] = Field(default_factory=list, max_length=3)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str = Field(min_length=1, max_length=500)


def _fail(failure: ToolFailure) -> HTTPException:
    return HTTPException(status_code=failure.status, detail=failure.message)


@router.post("", status_code=201)
@limiter.limit("20/minute")
async def ask(request: Request, body: AskIn, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        thread_id = body.thread_id
        if not thread_id:
            thread_id = (await threads.create(ctx.workspace_id, ctx.user_id, "app", title=body.message))["id"]
        return await runs.create(ctx.workspace_id, ctx.user_id, body.message, body.trust, thread_id=thread_id, reference_ids=body.reference_ids)
    except ToolFailure as failure:
        raise _fail(failure)


_STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
_stream_tasks: set[asyncio.Task] = set()
STREAM_SECONDS = 600
PING_SECONDS = 12


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/stream")
@limiter.limit("20/minute")
async def ask_stream(request: Request, body: AskIn, ctx: WorkspaceContext = Depends(get_current_workspace)) -> StreamingResponse:
    """The same as asking, but each stage is sent as it happens (finding tools, understanding, planning, checking the price) and then the finished request, so the
    screen can show Ekavo working instead of a spinner. Events: `open` (the conversation id), `phase` (key and words), `run` (the request), `problem` (a plain message)."""
    thread_id = body.thread_id
    if not thread_id:
        thread_id = (await threads.create(ctx.workspace_id, ctx.user_id, "app", title=body.message))["id"]
    queue: asyncio.Queue = asyncio.Queue()

    async def phase(key: str) -> None:
        await queue.put(("phase", {"key": key, "label": runs.PHASES.get(key, key)}))

    async def work() -> None:
        try:
            await queue.put(("run", await runs.create(ctx.workspace_id, ctx.user_id, body.message, body.trust, thread_id=thread_id, on_phase=phase, reference_ids=body.reference_ids)))
        except ToolFailure as failure:
            await queue.put(("problem", {"message": failure.message, "status": failure.status}))
        except Exception:  # noqa: BLE001 - out of credits and the like are reported plainly; nothing escapes a background task
            await queue.put(("problem", {"message": "I could not think that through just now. Nothing was charged for the failed step. Try again in a minute.", "status": 503}))
        finally:
            await queue.put(None)

    task = asyncio.create_task(work())
    _stream_tasks.add(task)                                       # the request is kept even if the screen goes away mid-way
    task.add_done_callback(_stream_tasks.discard)

    async def events():
        yield _sse("open", {"thread_id": thread_id})
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield _sse(item[0], item[1])

    return StreamingResponse(events(), media_type="text/event-stream", headers=_STREAM_HEADERS)


@router.get("/{run_id}/stream")
@limiter.limit("30/minute")
async def run_stream(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> StreamingResponse:
    """Follows a request while it works: the request is sent again each time its steps change, and the stream ends when it is no longer running."""
    async def events():
        last = None
        waited = 0.0
        quiet = 0.0
        while waited < STREAM_SECONDS:
            if await request.is_disconnected():
                break
            try:
                run = await runs.get(ctx.workspace_id, ctx.user_id, run_id)
            except ToolFailure as failure:
                yield _sse("problem", {"message": failure.message, "status": failure.status})
                break
            key = json.dumps([run.get("status"), run.get("result")], default=str, sort_keys=True)
            if key != last:
                last, quiet = key, 0.0
                yield _sse("run", run)
            if run.get("status") != "running":
                break
            await asyncio.sleep(0.7)
            waited += 0.7
            quiet += 0.7
            if quiet >= PING_SECONDS:
                quiet = 0.0
                yield ": ping\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=_STREAM_HEADERS)


MAX_VOICE_BYTES = 10 * 1024 * 1024


@router.post("/references")
@limiter.limit("20/minute")
async def add_reference(request: Request, file: UploadFile = File(...), ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """A picture to follow the look of for one request. Only its look is kept (no model, no cost, 24 hours); send the returned id as `reference_ids` with the request."""
    data = await file.read(request_refs.MAX_BYTES + 1)
    try:
        return await request_refs.add(ctx.workspace_id, ctx.user_id, data)
    except request_refs.RefError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.post("/voice")
@limiter.limit("20/minute")
async def voice(request: Request, file: UploadFile = File(...), ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    """Turns a spoken request into words, with the same speech chain as the chat apps, so the person can check the words before sending them.
    Free: nothing is asked of a planner and no credits are used. Any language the speech chain hears works."""
    data = await file.read(MAX_VOICE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=422, detail="Nothing was recorded.")
    if len(data) > MAX_VOICE_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too long. Keep it under a couple of minutes.")
    name = (file.filename or "voice.webm").rsplit("/", 1)[-1][:60] or "voice.webm"
    try:
        text, language = await channel_engine.default_transcribe(data, name)
    except Exception:  # noqa: BLE001 - every speech provider is down: retryable, and nothing was charged
        raise HTTPException(status_code=503, detail="Speech to text is busy right now. Nothing was charged. Try again in a minute.")
    if not text:
        raise HTTPException(status_code=422, detail="I could not hear anything in that. Try again a little closer to the microphone.")
    return {"text": text[: runs.MAX_MESSAGE], "language": language}


@router.get("")
@limiter.limit("60/minute")
async def history(
    request: Request, q: str = Query("", max_length=80), status: str = Query("", max_length=20), limit: int = Query(20, ge=1, le=50),
    before: Optional[datetime] = Query(None), ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict[str, Any]:
    """The person's own requests, newest first: search by words, narrow by status group, page back with `before`."""
    try:
        return await runs.history(ctx.workspace_id, ctx.user_id, limit=limit, q=q, group=status, before=before)
    except ToolFailure as failure:
        raise _fail(failure)


@router.get("/{run_id}")
@limiter.limit("240/minute")
async def one(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.get(ctx.workspace_id, ctx.user_id, run_id)
    except ToolFailure as failure:
        raise _fail(failure)


@router.post("/{run_id}/confirm")
@limiter.limit("30/minute")
async def confirm(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.confirm(ctx.workspace_id, ctx.user_id, run_id)
    except ToolFailure as failure:
        raise _fail(failure)


@router.post("/{run_id}/answer", status_code=201)
@limiter.limit("20/minute")
async def answer(request: Request, run_id: str, body: AnswerIn, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.answer(ctx.workspace_id, ctx.user_id, run_id, body.reply)
    except ToolFailure as failure:
        raise _fail(failure)


@router.post("/{run_id}/stop")
@limiter.limit("60/minute")
async def stop(request: Request, run_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        return await runs.stop(ctx.workspace_id, ctx.user_id, run_id)
    except ToolFailure as failure:
        raise _fail(failure)
