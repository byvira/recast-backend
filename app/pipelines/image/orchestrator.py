"""Runs the image agent. The two public entry points in `app.pipelines.media.image_generation` call this, so every picture the
product makes goes through the same planned, logged and traced workflow."""
from __future__ import annotations

from typing import Any, Optional

from app.agents.base import initial_state
from app.agents.image.graph import image_agent

_RUN_NAME = "image_generation_pipeline"


async def run_image_agent(
    *, mode: str, workspace_id: str, user_id: str, brand: Optional[dict], topic: str = "", prompt: str = "", avoid: Optional[str] = None,
) -> dict[str, Any]:
    """Returns the agent's outputs: image bytes, the library item (topic mode), a plain failure reason when nothing was made,
    the polished prompt, the QA flag and the gate log. Never raises for a tracing reason."""
    from app.core.tracing import ainvoke_traced
    from app.shared.entitlements import picture_room_message

    # Every AI picture counts against the daily allowance, whichever screen, campaign, agent or API asked for it.
    full = await picture_room_message(workspace_id)
    if full:
        return {"image": None, "asset": None, "failure": full}

    state = initial_state(
        user_id=user_id, brand=brand or {}, max_retries=0,
        raw_input={"mode": mode, "workspace_id": workspace_id, "user_id": user_id, "topic": topic, "prompt": prompt, "avoid": avoid},
    )
    final, _ = await ainvoke_traced(
        image_agent, state, run_name=_RUN_NAME, agent="image", workspace_id=workspace_id, user_id=user_id,
    )
    return final["outputs"]
