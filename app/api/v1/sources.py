"""Sources as a member sees them: follow a feed or a page, see what it would give before switching it on, and pause, resume or remove it.

A source only ever makes drafts, as the member who made it (see `app/sources/`). Everything here is done by a signed-in member in the app, with the right to create content."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.sources import service

router = APIRouter()
_CREATE = require("create_content")


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = Field(min_length=8, max_length=2000)
    kind: Literal["feed", "page"] = "feed"
    name: Optional[str] = Field(None, max_length=80)
    brand_id: str = Field(min_length=1, max_length=64)
    platforms: list[str] = Field(min_length=1, max_length=6)
    mode: Literal["link", "write"] = "link"
    relevance: Literal["everything", "medium", "close"] = "medium"
    daily_limit: int = Field(10, ge=1, le=30)
    check_every_hours: int = Field(6, ge=6, le=168)
    include_older: bool = False


class SourceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, max_length=80)
    platforms: Optional[list[str]] = Field(None, min_length=1, max_length=6)
    mode: Optional[Literal["link", "write"]] = None
    relevance: Optional[Literal["everything", "medium", "close"]] = None
    daily_limit: Optional[int] = Field(None, ge=1, le=30)
    check_every_hours: Optional[int] = Field(None, ge=6, le=168)


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = Field(min_length=8, max_length=2000)
    kind: Literal["feed", "page"] = "feed"


def _fail(exc: service.SourceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("")
@limiter.limit("60/minute")
async def list_sources(request: Request, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    return {"items": await service.list_sources(ctx.workspace_id), "limit": service.MAX_SOURCES, "min_hours": service.MIN_HOURS}


@router.post("/check")
@limiter.limit("10/minute")
async def check(request: Request, body: CheckIn, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    """Fetch an address once and show its first items. Nothing is made and nothing is followed."""
    try:
        return await service.check(ctx.workspace_id, body.address, body.kind)
    except service.SourceError as exc:
        raise _fail(exc)


MAX_FILE_BYTES = 10 * 1024 * 1024


async def _file_bytes(file: UploadFile) -> bytes:
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="That file is larger than 10 MB. Use a part of it.")
    return data


class ShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, max_length=300)
    text: Optional[str] = Field(None, max_length=6000)
    url: Optional[str] = Field(None, max_length=2000)
    brand_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=30)


@router.post("/share")
@limiter.limit("30/minute")
async def share(request: Request, body: ShareIn, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    """Save something shared from the phone's share sheet (a link or some text) as a plain draft. Free: no model, no credits, nothing posted."""
    from app.sources import share as shared

    try:
        return await shared.save(ctx.workspace_id, ctx.user_id, body.model_dump())
    except service.SourceError as exc:
        raise _fail(exc)


@router.post("/file/check")
@limiter.limit("20/minute")
async def check_file(request: Request, file: UploadFile = File(...), ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    """Look inside a file without making anything: what it is, how many items it would give, the columns of a spreadsheet and the first few items."""
    try:
        return await service.preview_file(ctx.workspace_id, file.filename or "", await _file_bytes(file))
    except service.SourceError as exc:
        raise _fail(exc)


@router.post("/file", status_code=201)
@limiter.limit("10/hour")
async def create_file(
    request: Request, file: UploadFile = File(...), brand_id: str = Form(..., max_length=64), platforms: str = Form(..., max_length=300), name: Optional[str] = Form(None, max_length=80),
    mode: Literal["link", "write"] = Form("link"), relevance: Literal["everything", "medium", "close"] = Form("medium"), daily_limit: int = Form(10, ge=1, le=30),
    title_column: Optional[str] = Form(None, max_length=100), text_columns: Optional[str] = Form(None, max_length=600), ctx: WorkspaceContext = Depends(_CREATE),
) -> dict[str, Any]:
    """Follow a file: a spreadsheet (each row becomes a draft idea), a document, or a picture. `platforms` and `text_columns` are comma separated lists."""
    body = {"brand_id": brand_id, "platforms": [p.strip() for p in platforms.split(",") if p.strip()], "name": name, "mode": mode, "relevance": relevance, "daily_limit": daily_limit,
            "title_column": title_column, "text_columns": [c.strip() for c in (text_columns or "").split(",") if c.strip()]}
    try:
        return await service.create_file(ctx.workspace_id, ctx.user_id, file.filename or "", await _file_bytes(file), body)
    except service.SourceError as exc:
        raise _fail(exc)


@router.post("", status_code=201)
@limiter.limit("20/minute")
async def create(request: Request, body: SourceIn, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    try:
        return await service.create(ctx.workspace_id, ctx.user_id, body.model_dump())
    except service.SourceError as exc:
        raise _fail(exc)


@router.get("/{source_id}")
@limiter.limit("120/minute")
async def one(request: Request, source_id: str, ctx: WorkspaceContext = Depends(get_current_workspace)) -> dict[str, Any]:
    try:
        from app.sources import results

        return {"source": await service.get(ctx.workspace_id, source_id), "items": await service.items_of(ctx.workspace_id, source_id), "results": await results.stats(ctx.workspace_id, source_id)}
    except service.SourceError as exc:
        raise _fail(exc)


class Rating(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["good", "not_for_me", "none"]


@router.post("/{source_id}/items/{item_id}/feedback")
@limiter.limit("120/minute")
async def rate(request: Request, source_id: str, item_id: str, body: Rating, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    """Tell Ekavo an item was a good one or not for you. It only helps the results and the suggestions; it never changes a setting."""
    try:
        return await service.rate_item(ctx.workspace_id, source_id, item_id, body.verdict)
    except service.SourceError as exc:
        raise _fail(exc)


@router.patch("/{source_id}")
@limiter.limit("30/minute")
async def change(request: Request, source_id: str, body: SourceChange, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    try:
        return await service.update(ctx.workspace_id, source_id, body.model_dump(exclude_none=True))
    except service.SourceError as exc:
        raise _fail(exc)


@router.post("/{source_id}/pause")
@limiter.limit("30/minute")
async def pause(request: Request, source_id: str, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    try:
        return await service.pause(ctx.workspace_id, source_id)
    except service.SourceError as exc:
        raise _fail(exc)


@router.post("/{source_id}/resume")
@limiter.limit("30/minute")
async def resume(request: Request, source_id: str, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    try:
        return await service.resume(ctx.workspace_id, source_id)
    except service.SourceError as exc:
        raise _fail(exc)


@router.post("/{source_id}/run")
@limiter.limit("6/hour")
async def run_now(request: Request, source_id: str, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    """Check this source now instead of waiting. Limited to a few an hour so it cannot be used to hammer someone's site."""
    try:
        source = await service._load(ctx.workspace_id, source_id)
    except service.SourceError as exc:
        raise _fail(exc)
    from app.tools.services import LiveServices

    held = await LiveServices().switched_off(ctx.workspace_id, "sources.run")
    if held:
        raise HTTPException(status_code=503, detail=held)
    if source.get("created_by") != ctx.user_id:
        raise HTTPException(status_code=403, detail="Only the person who set this source up can run it, because it works as them.")
    return await service.run(source, force=True)


@router.delete("/{source_id}")
@limiter.limit("30/minute")
async def remove(request: Request, source_id: str, ctx: WorkspaceContext = Depends(_CREATE)) -> dict[str, Any]:
    try:
        await service.remove(ctx.workspace_id, source_id)
    except service.SourceError as exc:
        raise _fail(exc)
    return {"ok": True}
