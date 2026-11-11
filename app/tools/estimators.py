"""How much of its price row a call will use, worked out from its typed input and the real length of what it works on, so a dry run shows
the same credits the work then charges. They count the way the app's own charge points do (`charges.minutes`, `charges.words_in`).

A tool priced by length that is not here (voiced dialogue, translating captions for many videos) is shown as one unit and marked "not
exact": the number is a floor, and the caller is told so."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from app.billing import charges
from app.db.mongo import get_db
from app.pipelines.media import duration as spoken
from app.tools import services


def _minutes(seconds: Optional[float], fallback_s: float = 60.0) -> float:
    return charges.minutes(seconds, fallback_s)


async def _media_seconds(workspace_id: str, media_id: Optional[str]) -> Optional[float]:
    if not media_id:
        return None
    row = await get_db()["media_assets"].find_one({"id": media_id, "workspace_id": workspace_id}, {"duration_s": 1})
    return float(row["duration_s"]) if row and row.get("duration_s") else None


async def _asset_minutes(arguments: BaseModel, workspace_id: str) -> float:
    asset_id = getattr(arguments, "audio_asset_id", None)
    row = await get_db()["audio_assets"].find_one({"id": asset_id, "workspace_id": workspace_id}, {"media_id": 1}) if asset_id else None
    return _minutes(await _media_seconds(workspace_id, (row or {}).get("media_id")))


async def _media_minutes(arguments: BaseModel, workspace_id: str) -> float:
    return _minutes(await _media_seconds(workspace_id, getattr(arguments, "media_id", None)))


def _spoken_minutes(script: str, wpm: Optional[float] = None) -> float:
    return _minutes(spoken.estimate_seconds(script, wpm), 1.0)


def _generate_minutes(arguments: BaseModel, workspace_id: str) -> float:
    return _spoken_minutes(getattr(arguments, "script", "") or "", getattr(arguments, "words_per_minute", None))


async def _narrate_minutes(arguments: BaseModel, workspace_id: str) -> float:
    script = getattr(arguments, "script", None)
    if not script:
        lines = getattr(arguments, "lines", None) or []
        script = " ".join(str(l.get("text", "")) if isinstance(l, dict) else str(getattr(l, "text", l)) for l in lines)
    if script:
        return _spoken_minutes(script)
    return await _media_minutes(arguments, workspace_id)


async def _render_minutes(arguments: BaseModel, workspace_id: str) -> float:
    row = await get_db()["video_projects"].find_one({"id": getattr(arguments, "project_id", None), "workspace_id": workspace_id}, {"edit.duration_f": 1, "edit.fps": 1})
    edit = (row or {}).get("edit") or {}
    fps = edit.get("fps") or {"num": 30, "den": 1}
    return _minutes((edit.get("duration_f") or 0) * fps["den"] / fps["num"] or None)


def register_all() -> None:
    reg = services.register_estimator
    reg("audio.generate", _generate_minutes)
    reg("videovoice.narrate", _narrate_minutes)
    for tool in ("audio.cleanup", "audio.assemble", "audio.transcribe", "audio.video", "audio.regenerate"):
        reg(tool, _asset_minutes)
    reg("media.analyze", _media_minutes)
    reg("videoanalysis.run", _media_minutes)
    reg("video.render", _render_minutes)


register_all()
