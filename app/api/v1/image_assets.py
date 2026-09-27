"""ImageAsset API routes — the real replacement for the retired
app/api/v1/image.py decoy. Generate + basic export for every layout with a
real pixel target (widened 2026-09-26 from the original quote_1_1/
hero_16_9-only Stage 2 — see image_render.SUPPORTED_LAYOUTS's own comment).
Follows media.py's/text.py's exact @limiter.limit/Depends(require(...))
conventions.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.api.v1.media import ALLOWED_MIME_TYPES, _max_bytes_for
from app.core.config import settings
from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import (
    brand_profiles,
    content_pieces,
    image_assets,
    image_asset_versions,
    image_share_links,
    media_assets,
)
from app.models.agent_events import ContentEventPayload, ContentRef, EventType
from app.models.image_asset import (
    ImageAsset,
    ImageApprovalStatus,
    ImageAssetVersion,
    ImageShareLink,
    ImageSourceType,
    LayoutPreset,
    Slide,
)
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.pipelines.media.image_generation import _build_raw_prompt, generate_image_from_prompt
from app.pipelines.media.image_render import (
    BrandTokens,
    LAYOUT_DIMS,
    SUPPORTED_LAYOUTS,
    SlideTextContent,
    render_slide,
)
from app.pipelines.media.transform import build_export_url
from app.shared.events import emit_event_background
from app.shared.pipeline_types import PipelineType
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_brand_profile(brand_id: str, workspace_id: str) -> dict:
    brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    return brand


async def _fetch_logo_bytes(logo_url: str) -> Optional[bytes]:
    """Real fetch of the brand's already-uploaded logo (Cloudinary URL from
    My Voices > Brand Assets) — never generated, never a placeholder. A
    fetch failure (network hiccup, deleted asset) skips the logo rather
    than failing the whole generation; the image is still real and usable
    without it."""
    if not logo_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(logo_url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("Logo fetch failed for %s, skipping: %s", logo_url, exc)
        return None


async def _render_and_upload_slide(
    *,
    prompt: str,
    workspace_id: str,
    user_id: str,
    layout: LayoutPreset,
    brand: dict,
    brand_tokens: BrandTokens,
    text_content: SlideTextContent,
    show_logo: bool,
) -> MediaAsset:
    """Shared generate+render+upload core, factored out of the original
    `/generate` body so `add_slide` (real multi-slide carousel CRUD) reuses
    the exact same real path rather than a second, drifting copy."""
    background_bytes = await generate_image_from_prompt(
        prompt=prompt,
        workspace_id=workspace_id,
        user_id=user_id,
        target_size=LAYOUT_DIMS[layout],
        brand_profile=brand,
    )
    visual_identity = brand.get("visual_identity") or {}
    logo_bytes = await _fetch_logo_bytes(visual_identity.get("logo_url") or "") if show_logo else None
    png_bytes = render_slide(
        layout=layout,
        base_image_bytes=background_bytes,
        brand_tokens=brand_tokens,
        text_content=text_content,
        logo_bytes=logo_bytes,
    )
    width, height = LAYOUT_DIMS[layout]
    url = await upload_file(png_bytes, UploadContentType.IMAGE, user_id)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=workspace_id,
        kind=MediaKind.IMAGE,
        url=url,
        mime_type="image/png",
        width=width,
        height=height,
        source=MediaSource.RENDERED,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    await media_assets.insert_one(media.model_dump())
    return media


def _brand_tokens_from(brand: dict) -> BrandTokens:
    visual_identity = brand.get("visual_identity") or {}
    colors = visual_identity.get("colors") or {}
    fonts = visual_identity.get("fonts") or {}
    return BrandTokens(
        primary_hex=colors.get("primary") or "#6366f1",
        secondary_hex=colors.get("secondary") or "#0f172a",
        accent_hex=colors.get("accent") or "#38bdf8",
        heading_font=fonts.get("heading") or None,
        body_font=fonts.get("body") or None,
    )


async def _bump_version(
    asset_id: str, workspace_id: str, new_slides: list[Slide], action: str, actor_user_id: str
) -> Optional[dict]:
    """Real versioning for multi-slide CRUD/governance actions — mirrors
    app.pipelines.text.storage.update_piece_content's "update doc, then
    insert a version snapshot of the new state" pattern (app.models.
    image_asset.ImageAssetVersion mirrors ContentPieceVersion by design)."""
    doc = await image_assets.find_one({"id": asset_id, "workspace_id": workspace_id})
    if not doc:
        return None
    new_version_number = doc.get("version_count", 1) + 1
    now = datetime.now(timezone.utc)
    slides_dump = [s.model_dump() for s in new_slides]
    await image_assets.update_one(
        {"id": asset_id, "workspace_id": workspace_id},
        {"$set": {"slides": slides_dump, "version_count": new_version_number, "updated_at": now}},
    )
    version = ImageAssetVersion(
        version_id=str(uuid4()),
        image_asset_id=asset_id,
        workspace_id=workspace_id,
        user_id=actor_user_id,
        version_number=new_version_number,
        slides_snapshot=new_slides,
        action=action,
        created_at=now,
    )
    await image_asset_versions.insert_one(version.model_dump())
    return await image_assets.find_one({"id": asset_id, "workspace_id": workspace_id})


def _piece_topic(piece: dict) -> str:
    """First non-empty line of a piece's real content, capped — same
    technique app.pipelines.media.default_image._hook_line uses, so a
    repurposed image starts from the piece's actual hook, not an invented
    caption."""
    for line in (piece.get("content") or "").splitlines():
        line = line.strip()
        if line:
            return line[:180]
    return ""


class GenerateImageAssetRequest(BaseModel):
    """Field names match the existing Image pipeline UI's own state
    verbatim (page.tsx) — prompt/negativePrompt/seed/visualProfile/
    activeLayout — per the plan's explicit "don't invent new field names"
    instruction. `prompt` is optional only when `source_piece_id` is given
    with no prompt yet reviewed — see the route's own docstring for the
    exact precedence."""

    title: str
    brand_id: str
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    visual_profile: str = "midnight_swiss"
    active_layout: LayoutPreset = LayoutPreset.QUOTE_1_1
    headline: str
    accent_keyword: str = ""
    author: Optional[str] = None
    source_piece_id: Optional[str] = None
    # Real logo compositing (G-3, resolved 2026-09-26) — opt-in: a brand
    # with a logo set doesn't automatically get it stamped onto every
    # image, since that's a real visual change the user should choose,
    # not one this build silently defaults on.
    show_logo: bool = False


@router.get("/layouts")
@limiter.limit("60/minute")
async def get_layouts(request: Request, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    """Real pixel dimensions per layout — the single source of truth the
    frontend should read instead of hardcoding its own copy (per the
    plan). All 9 layouts now have a real, confirmed target size (widened
    2026-09-26 — see image_render.LAYOUT_DIMS's own comment)."""
    return {layout.value: {"width": w, "height": h} for layout, (w, h) in LAYOUT_DIMS.items()}


@router.post("/generate", response_model=ImageAsset, status_code=201)
@limiter.limit("20/minute")
async def generate_image_asset(
    request: Request,
    body: GenerateImageAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ImageAsset:
    """Real end-to-end generate: AI background (Cloudflare/Gemini, via
    generate_image_from_prompt) composited with real brand colors + the
    real headline text into one PNG (image_render.render_slide), uploaded,
    persisted as a real ImageAsset + its one Slide's MediaAsset.

    Supports all 9 layouts (widened 2026-09-26 — see
    image_render.SUPPORTED_LAYOUTS's own comment for the exact history).

    source_piece_id (file 04 Part 1 repurpose-flow hookup): when given and
    `prompt` is omitted, the prompt is auto-built from that piece's real
    content + this brand's real profile (the same raw-prompt technique
    generate_brand_image already uses for topic-based generation) —
    `source_content_hash` is set either way so Phase 3's stale-upstream
    check has something to compare against later. If `prompt` IS given,
    it's used verbatim — an explicit edit always wins over auto-derivation.
    """
    if body.active_layout not in SUPPORTED_LAYOUTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Layout '{body.active_layout.value}' isn't supported yet — "
                f"only {sorted(l.value for l in SUPPORTED_LAYOUTS)} today."
            ),
        )

    brand = await _get_brand_profile(body.brand_id, ctx.workspace_id)

    source_content_hash: Optional[str] = None
    prompt = (body.prompt or "").strip()
    if body.source_piece_id:
        piece = await content_pieces.find_one(
            {"piece_id": body.source_piece_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}
        )
        if not piece:
            raise HTTPException(status_code=404, detail="Source piece not found.")
        content = piece.get("content") or ""
        source_content_hash = str(hash(content))
        if not prompt:
            prompt = _build_raw_prompt(_piece_topic(piece), brand)

    if not prompt:
        raise HTTPException(
            status_code=400, detail="prompt is required (or pass source_piece_id to derive one)."
        )

    # Real brand colors, read-only here by design — see PROGRESS.md's
    # Decisions Log 2026-09-26 entry. The client cannot override these via
    # this request; they always come from the real, live Brand Assets tab.
    visual_identity = brand.get("visual_identity") or {}
    colors = visual_identity.get("colors") or {}
    fonts = visual_identity.get("fonts") or {}
    brand_tokens = BrandTokens(
        primary_hex=colors.get("primary") or "#6366f1",
        secondary_hex=colors.get("secondary") or "#0f172a",
        accent_hex=colors.get("accent") or "#38bdf8",
        heading_font=fonts.get("heading") or None,
        body_font=fonts.get("body") or None,
    )

    background_bytes = await generate_image_from_prompt(
        prompt=prompt,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        target_size=LAYOUT_DIMS[body.active_layout],
        brand_profile=brand,
    )

    logo_bytes = await _fetch_logo_bytes(visual_identity.get("logo_url") or "") if body.show_logo else None

    png_bytes = render_slide(
        layout=body.active_layout,
        base_image_bytes=background_bytes,
        brand_tokens=brand_tokens,
        text_content=SlideTextContent(
            headline=body.headline, accent_keyword=body.accent_keyword, author=body.author
        ),
        logo_bytes=logo_bytes,
    )

    width, height = LAYOUT_DIMS[body.active_layout]
    url = await upload_file(png_bytes, UploadContentType.IMAGE, ctx.user_id)
    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.IMAGE,
        url=url,
        mime_type="image/png",
        width=width,
        height=height,
        source=MediaSource.RENDERED,
        created_by=ctx.user_id,
        created_at=now,
    )
    await media_assets.insert_one(media.model_dump())

    asset_id = str(uuid4())
    asset = ImageAsset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        brand_id=body.brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=body.title,
        source_type=ImageSourceType.AI_GENERATED,
        prompt=prompt,
        negative_prompt=body.negative_prompt,
        seed=body.seed,
        visual_profile=body.visual_profile,
        brand_tokens=brand_tokens.model_dump(),
        slides=[
            Slide(
                slide_number=1,
                title=body.title,
                slide_type=body.active_layout.value,
                layout=body.active_layout,
                media_id=media.id,
                text_content=SlideTextContent(
                    headline=body.headline, accent_keyword=body.accent_keyword, author=body.author
                ).model_dump(),
            )
        ],
        approval_status=ImageApprovalStatus.PENDING,
        source_piece_id=body.source_piece_id,
        source_content_hash=source_content_hash,
    )
    await image_assets.insert_one(asset.model_dump())

    # file 04 Part 2 — first-class Phase 1 scope, not deferred: this is
    # what makes Odette's digest and Remy's signal history see Image
    # content at all, on the same existing event bus text already uses.
    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.IMAGE,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=asset_id,
            content_ref=ContentRef(collection="image_assets", id=asset_id),
            content_text=asset.alt_text or prompt,
            content_summary=prompt[:400],
            brand_id=body.brand_id,
        ),
    )

    return asset


# Real starting point without any AI generation at all, mirroring
# audio_assets.py's own upload_audio_asset — see
# pow/audio_image_pipeline/GAPS.md G-12. Reuses media.py's own real
# mime-type/size validation directly rather than duplicating it.
_IMAGE_MIME_TYPES = ALLOWED_MIME_TYPES[MediaKind.IMAGE]


@router.post("/upload", response_model=ImageAsset, status_code=201)
@limiter.limit("20/minute")
async def upload_image_asset(
    request: Request,
    title: str = Form(...),
    brand_id: str = Form(...),
    file: UploadFile = File(...),
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ImageAsset:
    """Real upload -> ImageAsset(source_type=UPLOADED), no AI generation,
    no prompt, no render layout — none of those apply to an already-made
    image someone brings themselves."""
    if (file.content_type or "") not in _IMAGE_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type '{file.content_type}'. Allowed: {', '.join(sorted(_IMAGE_MIME_TYPES))}.",
        )

    contents = await file.read()
    max_bytes = await _max_bytes_for(MediaKind.IMAGE, ctx.workspace_id)
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=400, detail=f"Image must be {max_bytes // (1024 * 1024)}MB or smaller."
        )

    try:
        url = await upload_file(contents, UploadContentType.IMAGE, ctx.user_id)
    except Exception as exc:  # noqa: BLE001 — same tolerance as media.py's own upload_media
        logger.error("Image upload failed for workspace %s: %s", ctx.workspace_id, exc)
        raise HTTPException(status_code=502, detail="Image upload failed. Try again.")

    now = datetime.now(timezone.utc)
    media = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.IMAGE,
        url=url,
        mime_type=file.content_type,
        source=MediaSource.UPLOADED,
        created_by=ctx.user_id,
        created_at=now,
    )
    await media_assets.insert_one(media.model_dump())

    asset_id = str(uuid4())
    asset = ImageAsset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        brand_id=brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=title,
        source_type=ImageSourceType.UPLOADED,
        slides=[
            Slide(
                slide_number=1,
                title=title,
                slide_type="upload",
                media_id=media.id,
            )
        ],
        approval_status=ImageApprovalStatus.PENDING,
    )
    await image_assets.insert_one(asset.model_dump())

    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.IMAGE,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=asset_id,
            content_ref=ContentRef(collection="image_assets", id=asset_id),
            content_text="",
            content_summary=title,
            brand_id=brand_id,
        ),
    )

    return asset


class ExportImageAssetRequest(BaseModel):
    export_format: str = "png"  # "png" | "webp" — see GAPS.md G-4 for svg/pdf/zip


@router.post("/{image_asset_id}/export", response_model=MediaAsset, status_code=201)
@limiter.limit("30/minute")
async def export_image_asset(
    request: Request,
    image_asset_id: str,
    body: ExportImageAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> MediaAsset:
    """Format-conversion export of an ImageAsset's one Stage-2 slide via
    Cloudinary URL params — same "create a real, distinct derivative"
    convention media.py's transform_media uses, not a mutation of the
    original render."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)
    if not asset.slides or not asset.slides[0].media_id:
        raise HTTPException(status_code=400, detail="This image asset has no rendered slide to export.")

    source_doc = await media_assets.find_one(
        {"id": asset.slides[0].media_id, "workspace_id": ctx.workspace_id}
    )
    if not source_doc:
        raise HTTPException(status_code=404, detail="The rendered slide's file is missing.")
    source = MediaAsset(**source_doc)

    try:
        new_url = build_export_url(source.url, export_format=body.export_format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    export_asset = MediaAsset(
        id=str(uuid4()),
        workspace_id=ctx.workspace_id,
        kind=MediaKind.IMAGE,
        url=new_url,
        mime_type=f"image/{body.export_format}",
        width=source.width,
        height=source.height,
        source=MediaSource.EDITED,
        created_by=ctx.user_id,
        created_at=datetime.now(timezone.utc),
    )
    await media_assets.insert_one(export_asset.model_dump())
    return export_asset


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-SLIDE CAROUSEL CRUD — closes the "carousel/slide-sequence management"
# gap PROGRESS.md's Deferred list named (reorder, per-slide CRUD). Every
# mutation goes through _bump_version so it's real, restorable history, not
# a silent overwrite — same governance model as Text's ContentPieceVersion.
# ─────────────────────────────────────────────────────────────────────────────

class AddSlideRequest(BaseModel):
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    active_layout: LayoutPreset = LayoutPreset.QUOTE_1_1
    headline: str
    accent_keyword: str = ""
    author: Optional[str] = None
    show_logo: bool = False


@router.post("/{image_asset_id}/slides", response_model=ImageAsset, status_code=201)
@limiter.limit("20/minute")
async def add_slide(
    request: Request,
    image_asset_id: str,
    body: AddSlideRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ImageAsset:
    """Generates and appends one new real slide to an existing carousel.
    Reuses the parent asset's own prompt when the caller doesn't give a
    new one, so adding slide 2 of a 5-slide carousel doesn't require
    re-typing the same prompt."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)

    if body.active_layout not in SUPPORTED_LAYOUTS:
        raise HTTPException(
            status_code=400, detail=f"Layout '{body.active_layout.value}' isn't supported."
        )

    brand = await _get_brand_profile(asset.brand_id, ctx.workspace_id)
    prompt = (body.prompt or asset.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required.")

    text_content = SlideTextContent(
        headline=body.headline, accent_keyword=body.accent_keyword, author=body.author
    )
    media = await _render_and_upload_slide(
        prompt=prompt,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        layout=body.active_layout,
        brand=brand,
        brand_tokens=_brand_tokens_from(brand),
        text_content=text_content,
        show_logo=body.show_logo,
    )

    next_number = max((s.slide_number for s in asset.slides), default=0) + 1
    new_slide = Slide(
        slide_number=next_number,
        title=body.headline[:80] or asset.title,
        slide_type=body.active_layout.value,
        layout=body.active_layout,
        media_id=media.id,
        text_content=text_content.model_dump(),
    )
    updated_doc = await _bump_version(
        image_asset_id, ctx.workspace_id, asset.slides + [new_slide], "slide_added", ctx.user_id
    )
    return ImageAsset(**updated_doc)


@router.delete("/{image_asset_id}/slides/{slide_number}", response_model=ImageAsset)
@limiter.limit("20/minute")
async def remove_slide(
    request: Request,
    image_asset_id: str,
    slide_number: int,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> ImageAsset:
    """Removes one slide and renumbers the rest sequentially (1..N, no
    gaps) — real deletion with real version history, not a soft hide."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)

    remaining = [s for s in asset.slides if s.slide_number != slide_number]
    if len(remaining) == len(asset.slides):
        raise HTTPException(status_code=404, detail=f"Slide {slide_number} not found.")
    if not remaining:
        raise HTTPException(status_code=400, detail="Can't remove the last slide of an image asset.")

    renumbered = [s.model_copy(update={"slide_number": i + 1}) for i, s in enumerate(remaining)]
    updated_doc = await _bump_version(image_asset_id, ctx.workspace_id, renumbered, "slide_removed", ctx.user_id)
    return ImageAsset(**updated_doc)


class ReorderSlidesRequest(BaseModel):
    order: list[int]  # the current slide_numbers, in the new desired order


@router.patch("/{image_asset_id}/slides/reorder", response_model=ImageAsset)
@limiter.limit("20/minute")
async def reorder_slides(
    request: Request,
    image_asset_id: str,
    body: ReorderSlidesRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> ImageAsset:
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)

    by_number = {s.slide_number: s for s in asset.slides}
    if set(body.order) != set(by_number.keys()):
        raise HTTPException(
            status_code=400, detail="order must contain exactly the current slide numbers, no more or fewer."
        )

    reordered = [by_number[n].model_copy(update={"slide_number": i + 1}) for i, n in enumerate(body.order)]
    updated_doc = await _bump_version(image_asset_id, ctx.workspace_id, reordered, "slides_reordered", ctx.user_id)
    return ImageAsset(**updated_doc)


# ─────────────────────────────────────────────────────────────────────────────
# GOVERNANCE — approve/reject/version-history/restore/share-link. Mirrors
# app/api/v1/content.py's real Text-pipeline pattern exactly (same verbs,
# same status semantics) rather than inventing a new governance shape for
# Image specifically.
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/{image_asset_id}/approve", response_model=ImageAsset)
@limiter.limit("30/minute")
async def approve_image_asset(
    request: Request,
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> ImageAsset:
    """Approve + pin the current first slide's render as the master —
    approved_master_media_id is what a future export/publish/share-link
    flow should read, not necessarily whatever slide is currently first
    in the array if that ever changes after approval."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)
    master_media_id = asset.slides[0].media_id if asset.slides else None

    await image_assets.update_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {
            "approval_status": ImageApprovalStatus.APPROVED.value,
            "approved_master_media_id": master_media_id,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    return ImageAsset(**updated)


@router.patch("/{image_asset_id}/reject", response_model=ImageAsset)
@limiter.limit("30/minute")
async def reject_image_asset(
    request: Request,
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(require("approve_content")),
) -> ImageAsset:
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    await image_assets.update_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {
            "approval_status": ImageApprovalStatus.REJECTED.value,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    updated = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    return ImageAsset(**updated)


@router.get("/{image_asset_id}/versions")
@limiter.limit("60/minute")
async def list_image_versions(
    request: Request,
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    versions = await image_asset_versions.find(
        {"image_asset_id": image_asset_id, "workspace_id": ctx.workspace_id}
    ).sort("version_number", 1).to_list(length=100)
    for v in versions:
        v.pop("_id", None)
    return {"image_asset_id": image_asset_id, "versions": versions, "total": len(versions)}


@router.post("/{image_asset_id}/restore/{version_number}", response_model=ImageAsset)
@limiter.limit("20/minute")
async def restore_image_version(
    request: Request,
    image_asset_id: str,
    version_number: int,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> ImageAsset:
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    version_doc = await image_asset_versions.find_one({
        "image_asset_id": image_asset_id,
        "workspace_id": ctx.workspace_id,
        "version_number": version_number,
    })
    if not version_doc:
        raise HTTPException(status_code=404, detail=f"Version {version_number} not found.")

    restored_slides = [Slide(**s) for s in version_doc["slides_snapshot"]]
    updated_doc = await _bump_version(
        image_asset_id, ctx.workspace_id, restored_slides, f"restored_from_v{version_number}", ctx.user_id
    )
    return ImageAsset(**updated_doc)


class ShareLinkResponse(BaseModel):
    token: str
    url: str
    expires_at: datetime


@router.post("/{image_asset_id}/share-link", response_model=ShareLinkResponse, status_code=201)
@limiter.limit("10/minute")
async def create_image_share_link(
    request: Request,
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ShareLinkResponse:
    """Real token + expiry, mirroring invites.py's own token/expiry
    pattern (ImageShareLink's own docstring). Known, honest limitation:
    no public `/share/[token]` frontend page consumes this token yet —
    that's a separate, larger scope (a genuinely public, unauthenticated
    route) not built in this pass; the real backend contract exists so
    that page can be added without another backend change."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")

    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=30)
    link = ImageShareLink(
        token=token,
        image_asset_id=image_asset_id,
        workspace_id=ctx.workspace_id,
        created_by=ctx.user_id,
        created_at=now,
        expires_at=expires_at,
    )
    await image_share_links.insert_one(link.model_dump())
    return ShareLinkResponse(token=token, url=f"{settings.FRONTEND_URL}/share/{token}", expires_at=expires_at)
