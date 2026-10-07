"""ImageAsset API routes — the real replacement for the retired
app/api/v1/image.py decoy. Generate + basic export for every layout with a
real pixel target (widened 2026-09-26 from the original quote_1_1/
hero_16_9-only Stage 2 — see image_render.SUPPORTED_LAYOUTS's own comment).
Follows media.py's/text.py's exact @limiter.limit/Depends(require(...))
conventions.
"""

import asyncio
import io
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional
from uuid import uuid4

import httpx
from PIL import Image
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, field_validator

from app.agents.supervisor.service import assert_ai_budget_available, assert_generation_allowed
from app.api.v1.media import ALLOWED_MIME_TYPES, _max_bytes_for
from app.prompts.registry import load_prompt
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
    users,
)
from app.models.agent_events import ContentEventPayload, ContentRef, EventType
from app.models.image_asset import (
    Layer,
    CommentPin,
    CommentPinCreate,
    CommentPinUpdate,
    ImageAsset,
    ImageApprovalStatus,
    ImageAssetVersion,
    ImageShareLink,
    ImageSourceType,
    LayoutPreset,
    Slide,
)
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.models.workspace import WorkspaceRole
from app.pipelines.media.contrast_check import ContrastResult, check_slide_contrast
from app.pipelines.media.image_generation import _build_raw_prompt, generate_image_from_prompt, last_failure_reason
from app.pipelines.media.image_render import (
    _DEFAULT_FG,
    BrandTokens,
    LAYOUT_DIMS,
    SUPPORTED_LAYOUTS,
    SlideTextContent,
)
from app.pipelines.media import image_pack
from app.pipelines.media.image_layers import MAX_LAYERS, default_layers, layer_assets_needed, render_layers
from app.pipelines.media.icons import is_known_icon, list_icons
from app.pipelines.media.pdf_export import generate_carousel_pdf
from app.pipelines.media.transform import build_export_url
from app.pipelines.media.zip_export import build_slides_zip
from app.shared.events import emit_event_background
from app.shared.llm import call_vision, set_usage_workspace
from app.shared.pipeline_types import PipelineType
from app.shared.storage import ContentType as UploadContentType, upload_file

logger = logging.getLogger(__name__)
router = APIRouter()


_DESCRIBE_IMAGE_PROMPT = load_prompt("media/image_describe")


async def _describe_uploaded_image(contents: bytes, mime_type: str, workspace_id: str) -> Optional[str]:
    """Best-effort description of an uploaded image (its alt text, and the
    only "text" an uploaded image has for Remy/Odette to read). Returns None
    — never raises, never blocks the upload — if the vision provider is
    unavailable, slow, or the workspace's AI budget is used up."""
    try:
        await assert_ai_budget_available(workspace_id)
        set_usage_workspace(workspace_id)
        text = await asyncio.wait_for(call_vision(_DESCRIBE_IMAGE_PROMPT, contents, mime_type), timeout=15)
    except Exception as exc:  # noqa: BLE001 — includes the budget's HTTPException
        logger.info("Image description skipped for workspace %s: %s", workspace_id, exc)
        return None
    text = (text or "").strip()
    return text[:500] or None


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



def _fallback_note(background_bytes: Optional[bytes]) -> Optional[str]:
    """When no AI picture was made the slide is a colour card with the headline on it. Say so, and why."""
    if background_bytes:
        return None
    reason = last_failure_reason() or "The picture could not be made."
    return f"No AI picture, so this is a text card. {reason}"


class _Rendered(NamedTuple):
    media: MediaAsset                     # the finished picture, drawn from the layers
    background_media_id: Optional[str]    # the clean picture (no words on it), kept so edits never need a new AI picture
    layers: list[Layer]


def _image_mime(data: bytes) -> str:
    try:
        return "image/png" if (Image.open(io.BytesIO(data)).format or "").upper() == "PNG" else "image/jpeg"
    except Exception:  # noqa: BLE001
        return "image/jpeg"


async def _store_image(data: bytes, *, workspace_id: str, user_id: str, size: tuple[int, int], mime: str, source: MediaSource,
                       flagged_reason: Optional[str] = None) -> MediaAsset:
    from app.agents.content_guard.media import assert_image_ok

    await assert_image_ok(data, mime, workspace_id=workspace_id, where="generation")
    url = await upload_file(data, UploadContentType.IMAGE, user_id)
    media = MediaAsset(
        id=str(uuid4()), workspace_id=workspace_id, kind=MediaKind.IMAGE, url=url, mime_type=mime,
        width=size[0], height=size[1], source=source, created_by=user_id, created_at=datetime.now(timezone.utc),
        qa_flagged=flagged_reason is not None, qa_flag_reason=flagged_reason,
    )
    await media_assets.insert_one(media.model_dump())
    return media


async def _media_bytes(media_id: Optional[str], workspace_id: str) -> Optional[bytes]:
    """The bytes of a file in this workspace's library, or None. Only files the workspace owns can be read this way."""
    if not media_id:
        return None
    doc = await media_assets.find_one({"id": media_id, "workspace_id": workspace_id})
    return await _fetch_logo_bytes(doc.get("url") or "") if doc else None


async def _fetch_layer_assets(layers: list[Layer], brand: dict, workspace_id: str) -> dict[str, bytes]:
    """The pictures the layers need: the brand's logo and mascot, and uploaded images from this workspace's library. A
    layer can never name an outside address, so nothing outside the brand and the workspace library is ever fetched."""
    visual_identity = brand.get("visual_identity") or {}
    wanted = layer_assets_needed(layers)

    async def one(layer: Layer) -> tuple[str, Optional[bytes]]:
        if layer.type == "logo":
            return layer.id, await _fetch_logo_bytes(visual_identity.get("logo_url") or "")
        if layer.type == "mascot":
            return layer.id, await _fetch_logo_bytes(visual_identity.get("mascot_url") or "")
        return layer.id, await _media_bytes(layer.media_id, workspace_id)

    return {lid: data for lid, data in await asyncio.gather(*(one(layer) for layer in wanted)) if data}


async def _draw_and_store(
    *, size: tuple[int, int], background_bytes: Optional[bytes], layers: list[Layer], brand: dict, brand_tokens: BrandTokens,
    workspace_id: str, user_id: str,
) -> MediaAsset:
    assets = await _fetch_layer_assets(layers, brand, workspace_id)
    png = await asyncio.to_thread(render_layers, size=size, background_bytes=background_bytes, layers=layers, brand=brand_tokens, assets=assets)
    note = _fallback_note(background_bytes)
    # Tamil and Hindi need a text-shaping engine to draw every combination correctly. If this server does not have one, say so on
    # the picture instead of letting a wrongly ordered letter go unnoticed.
    from app.pipelines.media.image_render import script_font_entry, shaping_available

    if not shaping_available() and any(script_font_entry(layer.text) for layer in layers if layer.type == "text" and layer.text):
        shaping_note = "Tamil or Hindi text on this server may have a few letters out of order. Check the headline."
        note = f"{note} {shaping_note}" if note else shaping_note
    # A layer that needed a picture (the brand logo, the mascot, an uploaded image) that could not be loaded was left off the
    # picture. That is worth a look, so it is noted on the picture and not just logged.
    missing = [layer for layer in layers if layer.type in ("logo", "mascot", "image") and not layer.hidden and layer.id not in assets]
    if missing:
        names = ", ".join(sorted({"the brand logo" if layer.type == "logo" else "the mascot" if layer.type == "mascot" else "an added picture" for layer in missing}))
        gap = f"{names[0].upper()}{names[1:]} couldn't be loaded, so it is missing from this picture."
        note = f"{note} {gap}" if note else gap
    return await _store_image(png, workspace_id=workspace_id, user_id=user_id, size=size, mime="image/png", source=MediaSource.RENDERED, flagged_reason=note)


async def _render_and_upload_slide(
    *,
    prompt: str,
    avoid: Optional[str] = None,
    workspace_id: str,
    user_id: str,
    layout: LayoutPreset,
    brand: dict,
    brand_tokens: BrandTokens,
    text_content: SlideTextContent,
    show_logo: bool,
    show_mascot: bool = False,
) -> _Rendered:
    """Shared generate and draw core for `/generate`, the rest of a pack and `add_slide`. The AI picture is kept clean
    (nothing drawn on it) and the headline, band, logo and so on become editable layers over it; the finished picture is
    drawn from those layers, so the editor and the saved file always agree."""
    size = LAYOUT_DIMS[layout]
    background_bytes = await generate_image_from_prompt(
        prompt=prompt, workspace_id=workspace_id, user_id=user_id, target_size=size, brand_profile=brand, avoid=avoid,
    )
    visual_identity = brand.get("visual_identity") or {}
    background_media_id: Optional[str] = None
    if background_bytes:
        background = await _store_image(background_bytes, workspace_id=workspace_id, user_id=user_id, size=size,
                                        mime=_image_mime(background_bytes), source=MediaSource.AI_GENERATED)
        background_media_id = background.id
    layers = default_layers(
        size=size, brand=brand_tokens, headline=text_content.headline, show_text=text_content.show_text,
        accent_keyword=text_content.accent_keyword, author=text_content.author,
        has_logo=bool(show_logo and visual_identity.get("logo_url")), has_mascot=bool(show_mascot and visual_identity.get("mascot_url")),
        icon_name=text_content.icon_name, illustration_accent=text_content.illustration_accent,
    )
    media = await _draw_and_store(size=size, background_bytes=background_bytes, layers=layers, brand=brand, brand_tokens=brand_tokens,
                                  workspace_id=workspace_id, user_id=user_id)
    return _Rendered(media, background_media_id, layers)


def _brand_tokens_from(brand: dict) -> BrandTokens:
    visual_identity = brand.get("visual_identity") or {}
    colors = visual_identity.get("colors") or {}
    fonts = visual_identity.get("fonts") or {}
    return BrandTokens(
        primary_hex=colors.get("primary") or "#e04a1f",
        secondary_hex=colors.get("secondary") or "#0f172a",
        accent_hex=colors.get("accent") or "#38bdf8",
        heading_font=fonts.get("heading") or None,
        body_font=fonts.get("body") or None,
    )


async def _record_first_version(asset: ImageAsset) -> None:
    """The picture's first state as version 1, written when it is made. Until now the first version was only written the
    first time the picture was edited, so a picture never edited had no history and a failure between an edit's two writes
    could leave the version count ahead of the history. Best effort: a failure here never blocks making the picture, the
    first edit still writes the baseline if it is missing."""
    try:
        await image_asset_versions.insert_one(ImageAssetVersion(
            version_id=str(uuid4()),
            image_asset_id=asset.id,
            workspace_id=asset.workspace_id,
            user_id=asset.created_by,
            version_number=1,
            slides_snapshot=list(asset.slides),
            action="created",
            created_at=asset.created_at,
        ).model_dump())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record the first version of picture %s: %s", asset.id, exc)


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
    current_version = doc.get("version_count", 1)
    now = datetime.now(timezone.utc)

    # generate/upload never write a version row for the asset's first state,
    # so without this the very first edit made "restore to the original"
    # impossible (Version 1 not found). Snapshot the pre-change state under
    # its own version number the first time it's about to be replaced.
    if not await image_asset_versions.find_one(
        {"image_asset_id": asset_id, "workspace_id": workspace_id, "version_number": current_version}
    ):
        await image_asset_versions.insert_one(ImageAssetVersion(
            version_id=str(uuid4()),
            image_asset_id=asset_id,
            workspace_id=workspace_id,
            user_id=doc.get("created_by") or actor_user_id,
            version_number=current_version,
            slides_snapshot=[Slide(**s) for s in doc.get("slides", [])],
            action="created" if current_version == 1 else f"v{current_version}_baseline",
            created_at=doc.get("created_at") or now,
        ).model_dump())

    slides_dump = [s.model_dump() for s in new_slides]
    # Take the next version number in the same step as the change. Reading the count first and writing count + 1 let two
    # saves that overlap both claim the same number. An approved master stays pinned: the new version sits beside it.
    ops: dict = {"$set": {"slides": slides_dump, "updated_at": now}, "$inc": {"version_count": 1}}
    from pymongo import ReturnDocument
    bumped = await image_assets.find_one_and_update(
        {"id": asset_id, "workspace_id": workspace_id}, ops,
        projection={"version_count": 1}, return_document=ReturnDocument.AFTER,
    )
    if not bumped:
        return None
    new_version_number = bumped.get("version_count", current_version + 1)
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
    """The opening of a piece's real content (its first lines, up to 600 characters), so the picture is made from what the
    post is about and not only from its first sentence."""
    lines = [ln.strip() for ln in (piece.get("content") or "").splitlines() if ln.strip()]
    return " ".join(lines)[:600]


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

    @field_validator("headline", "accent_keyword", "author")
    @classmethod
    def _tidy_picture_words(cls, value):
        from app.agents.content_guard.media import tidy_picture_text

        return tidy_picture_text(value)

    # A Lucide icon name from GET /image-assets/icons, drawn above the headline.
    icon_name: Optional[str] = None
    # A large, faint icon behind the text (the chosen icon, or a default one).
    illustration_accent: bool = False
    source_piece_id: Optional[str] = None
    # How many images to make in one run (1 to 10). Each is a real generation and uses provider
    # quota. The first uses the prompt as written; the rest are varied takes on it, added as
    # more slides of the same image set.
    count: int = Field(1, ge=1, le=image_pack.MAX_PACK_SIZE)
    # Real logo compositing (G-3, resolved 2026-09-26) — opt-in: a brand
    # with a logo set doesn't automatically get it stamped onto every
    # image, since that's a real visual change the user should choose,
    # not one this build silently defaults on.
    # None means "automatic": a picture made from a post gets the brand's logo when the brand has one; a picture made by
    # hand keeps the old behaviour (off unless asked). True or False is always obeyed.
    show_logo: Optional[bool] = None
    # False makes a picture with no headline or band on it.
    show_text: bool = True
    # The brand's mascot in a corner of the picture. Off unless asked.
    show_mascot: bool = False


@router.get("/layouts")
@limiter.limit("60/minute")
async def get_layouts(request: Request, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    """Real pixel dimensions per layout — the single source of truth the
    frontend should read instead of hardcoding its own copy (per the
    plan). All 9 layouts now have a real, confirmed target size (widened
    2026-09-26 — see image_render.LAYOUT_DIMS's own comment)."""
    return {layout.value: {"width": w, "height": h} for layout, (w, h) in LAYOUT_DIMS.items()}


@router.get("/icons")
@limiter.limit("60/minute")
async def get_icons(request: Request, ctx: WorkspaceContext = Depends(require("create_content"))) -> dict:
    """Every icon that can be drawn on an image: {name, cp, tags}. `cp` is the
    character code of the icon in the bundled icon font, which the browser can
    load too, so the picker shows exactly what will be rendered."""
    return {"icons": list_icons()}


def _check_icon(name: Optional[str]) -> None:
    if name and not is_known_icon(name):
        raise HTTPException(status_code=400, detail="That icon isn't available. Pick one from the list.")


# Pictures being made right now in this process, keyed by who asked and the exact settings. A pack takes a while and the
# screen gives up waiting before the server does, so a second click or a retry used to start the same pack again, with the
# provider cost and a duplicate picture. While one is running, the same request is answered plainly instead.
_GENERATE_IN_FLIGHT: set[str] = set()


async def _generate_guard(body: GenerateImageAssetRequest, ctx: WorkspaceContext = Depends(require("create_content"))):
    key = f"{ctx.workspace_id}:{ctx.user_id}:{body.model_dump_json()}"
    if key in _GENERATE_IN_FLIGHT:
        raise HTTPException(status_code=409, detail="This picture is already being made. It will show in your history when it is ready.")
    _GENERATE_IN_FLIGHT.add(key)
    try:
        yield
    finally:
        _GENERATE_IN_FLIGHT.discard(key)


@router.post("/generate", response_model=ImageAsset, status_code=201)
@limiter.limit("20/minute")
async def generate_image_asset(
    request: Request,
    body: GenerateImageAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
    _guard: None = Depends(_generate_guard),
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
    return await create_image_asset(body, ctx)


@router.post("/generate/background", status_code=202)
@limiter.limit("20/minute")
async def generate_image_asset_in_background(
    request: Request,
    body: GenerateImageAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
    _guard: None = Depends(_generate_guard),
) -> dict:
    """The same work as POST /generate, but it returns at once with a saved run. The member can leave the page; the run's
    progress and cancel are on /api/v1/runs/{id}, and when it finishes the Activity Log links to the picture."""
    from app.shared import pipeline_runs

    await pipeline_runs.assert_capacity(ctx.workspace_id)
    doc = await pipeline_runs.create_run(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, kind="image", title=f"Picture \"{body.title}\"", steps_total=1,
    )

    async def work(run: dict) -> dict:
        await pipeline_runs.checkpoint(run["id"])
        asset = await create_image_asset(body, ctx)
        return {"asset_id": asset.id, "href": f"/dashboard/pipelines/image?asset={asset.id}"}

    pipeline_runs.start(doc, work)
    return pipeline_runs.public(doc)


async def create_image_asset(body: GenerateImageAssetRequest, ctx: WorkspaceContext) -> ImageAsset:
    """The work behind POST /generate, callable without a request (campaign runs use it)."""
    _check_icon(body.icon_name)
    await assert_generation_allowed(ctx.workspace_id)

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
        primary_hex=colors.get("primary") or "#e04a1f",
        secondary_hex=colors.get("secondary") or "#0f172a",
        accent_hex=colors.get("accent") or "#38bdf8",
        heading_font=fonts.get("heading") or None,
        body_font=fonts.get("body") or None,
    )

    use_logo = body.show_logo if body.show_logo is not None else bool(body.source_piece_id)
    rendered = await _render_and_upload_slide(
        prompt=prompt, avoid=body.negative_prompt, workspace_id=ctx.workspace_id, user_id=ctx.user_id, layout=body.active_layout, brand=brand, brand_tokens=brand_tokens,
        text_content=SlideTextContent(
            headline=body.headline, accent_keyword=body.accent_keyword, author=body.author,
            icon_name=body.icon_name, illustration_accent=body.illustration_accent, show_text=body.show_text,
            show_logo=use_logo, show_mascot=body.show_mascot,
        ),
        show_logo=use_logo, show_mascot=body.show_mascot,
    )
    media = rendered.media
    now = datetime.now(timezone.utc)

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
                background_media_id=rendered.background_media_id,
                layers=rendered.layers,
                text_content=SlideTextContent(
                    headline=body.headline, accent_keyword=body.accent_keyword, author=body.author,
            icon_name=body.icon_name, illustration_accent=body.illustration_accent, show_text=body.show_text,
            show_logo=use_logo, show_mascot=body.show_mascot,
                ).model_dump(),
            )
        ],
        approval_status=ImageApprovalStatus.PENDING,
        source_piece_id=body.source_piece_id,
        source_content_hash=source_content_hash,
        # A text card made because the AI picture failed carries that note on the picture; it is also kept on the asset, where
        # the review checks look for it (it was only ever on the picture's file record).
        # While the rest of a set is still being made the asset says so, so a server restart in the middle leaves a picture set
        # that is marked unfinished and not one that looks complete. Cleared below when the set is done.
        qa_flagged=bool(getattr(media, "qa_flagged", False)) or body.count > 1,
        qa_flag_reason=getattr(media, "qa_flag_reason", None) or ("Still making the rest of this set." if body.count > 1 else None),
    )
    await image_assets.insert_one(asset.model_dump())
    await _record_first_version(asset)

    # The rest of a pack: one more real image each, in order. If one fails (quota, provider) the
    # run stops and the set keeps what was made, so the member gets "3 of 4" and not nothing.
    if body.count > 1:
        extra_prompts = image_pack.pack_prompts(prompt, body.count)[1:]
        made = list(asset.slides)
        for position, extra_prompt in enumerate(extra_prompts, start=2):
            try:
                extra = await _render_and_upload_slide(
                    prompt=extra_prompt, workspace_id=ctx.workspace_id, user_id=ctx.user_id,
                    layout=body.active_layout, brand=brand, brand_tokens=brand_tokens,
                    text_content=SlideTextContent(
                        headline=body.headline, accent_keyword=body.accent_keyword, author=body.author,
                        icon_name=body.icon_name, illustration_accent=body.illustration_accent, show_text=body.show_text,
            show_logo=use_logo, show_mascot=body.show_mascot,
                    ),
                    show_logo=use_logo,
                )
            except Exception as exc:  # noqa: BLE001 - keep what was made
                logger.warning("Image pack stopped at image %d of %d for asset %s: %s", position, body.count, asset_id, exc)
                break
            made.append(Slide(
                slide_number=position, title=f"{body.title} ({position})", slide_type=body.active_layout.value,
                layout=body.active_layout, media_id=extra.media.id, background_media_id=extra.background_media_id, layers=extra.layers,
                text_content=SlideTextContent(
                    headline=body.headline, accent_keyword=body.accent_keyword, author=body.author,
                    icon_name=body.icon_name, illustration_accent=body.illustration_accent, show_text=body.show_text,
            show_logo=use_logo, show_mascot=body.show_mascot,
                ).model_dump(),
            ))
        if len(made) > len(asset.slides):
            updated_doc = await _bump_version(asset_id, ctx.workspace_id, made, "pack_generated", ctx.user_id)
            asset = ImageAsset(**updated_doc)
        if len(made) >= body.count:
            # The whole set was made: back to the picture's own note (if it has one).
            await image_assets.update_one(
                {"id": asset_id, "workspace_id": ctx.workspace_id},
                {"$set": {"qa_flagged": bool(getattr(media, "qa_flagged", False)), "qa_flag_reason": getattr(media, "qa_flag_reason", None)}},
            )
            asset = asset.model_copy(update={"qa_flagged": bool(getattr(media, "qa_flagged", False)), "qa_flag_reason": getattr(media, "qa_flag_reason", None)})
        if len(made) < body.count:
            # Say so on the asset: the member asked for a set and got part of it. Stored, not just logged.
            partial = f"Only {len(made)} of {body.count} pictures could be made. The rest were not made, so generate again to add more."
            await image_assets.update_one(
                {"id": asset_id, "workspace_id": ctx.workspace_id},
                {"$set": {"qa_flagged": True, "qa_flag_reason": partial}},
            )
            asset = asset.model_copy(update={"qa_flagged": True, "qa_flag_reason": partial})

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

    from app.agents.content_guard.media import assert_image_ok

    await assert_image_ok(contents, file.content_type or "image/jpeg", workspace_id=ctx.workspace_id, where="upload")

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

    # An uploaded image has no prompt, so nothing in the app could say what
    # it shows. A short description (best-effort) becomes its alt text and
    # the text Remy and Odette read.
    description = await _describe_uploaded_image(contents, file.content_type, ctx.workspace_id)

    asset_id = str(uuid4())
    asset = ImageAsset(
        id=asset_id,
        workspace_id=ctx.workspace_id,
        brand_id=brand_id,
        created_by=ctx.user_id,
        created_at=now,
        updated_at=now,
        title=title,
        alt_text=description,
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
    await _record_first_version(asset)

    emit_event_background(
        event_type=EventType.CONTENT_CREATED,
        pipeline_type=PipelineType.IMAGE,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user_id,
        actor_role=ctx.role,
        payload=ContentEventPayload(
            content_id=asset_id,
            content_ref=ContentRef(collection="image_assets", id=asset_id),
            content_text=description or "",
            content_summary=description or title,
            brand_id=brand_id,
        ),
    )

    return asset


class ExportImageAssetRequest(BaseModel):
    export_format: str = "png"  # "png" | "webp" | "pdf" | "zip"


async def _download_slide_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


@router.post("/{image_asset_id}/export", response_model=MediaAsset, status_code=201)
@limiter.limit("30/minute")
async def export_image_asset(
    request: Request,
    image_asset_id: str,
    body: ExportImageAssetRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> MediaAsset:
    """Format-conversion export (png/webp, via Cloudinary URL params — same
    "create a real, distinct derivative" convention media.py's
    transform_media uses) for a single slide, or a real multi-slide bundle
    (pdf/zip, every real rendered slide, not just the first)."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)
    if not asset.slides or not asset.slides[0].media_id:
        raise HTTPException(status_code=400, detail="This image asset has no rendered slide to export.")

    if body.export_format in ("pdf", "zip"):
        ids = [s.media_id for s in asset.slides if s.media_id]
        found = await media_assets.find({"id": {"$in": ids}}).to_list(length=len(ids))
        by_id = {m["id"]: m for m in found}
        ordered = [by_id[i] for i in ids if i in by_id]
        if not ordered:
            raise HTTPException(status_code=404, detail="This asset's rendered slides are missing.")
        try:
            slide_bytes = [await _download_slide_bytes(m["url"]) for m in ordered]
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Couldn't fetch one of this asset's slides. Try again.") from exc

        if body.export_format == "pdf":
            bundle = generate_carousel_pdf(slide_bytes)
            mime_type = "application/pdf"
        else:
            zip_entries = [(f"slide_{i + 1}.png", data) for i, data in enumerate(slide_bytes)]
            bundle = build_slides_zip(zip_entries)
            mime_type = "application/zip"

        url = await upload_file(bundle, UploadContentType.EXPORT, ctx.user_id)
        export_asset = MediaAsset(
            id=str(uuid4()), workspace_id=ctx.workspace_id, kind=MediaKind.DOCUMENT, url=url,
            mime_type=mime_type, source=MediaSource.EDITED, created_by=ctx.user_id,
            created_at=datetime.now(timezone.utc), size_bytes=len(bundle),
        )
        await media_assets.insert_one(export_asset.model_dump())
        return export_asset

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

    @field_validator("headline", "accent_keyword", "author")
    @classmethod
    def _tidy_picture_words(cls, value):
        from app.agents.content_guard.media import tidy_picture_text

        return tidy_picture_text(value)

    icon_name: Optional[str] = None
    illustration_accent: bool = False
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
    _check_icon(body.icon_name)

    if body.active_layout not in SUPPORTED_LAYOUTS:
        raise HTTPException(
            status_code=400, detail=f"Layout '{body.active_layout.value}' isn't supported."
        )

    brand = await _get_brand_profile(asset.brand_id, ctx.workspace_id)
    prompt = (body.prompt or asset.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required.")

    first_tc = (asset.slides[0].text_content if asset.slides else {}) or {}
    inherit_logo = first_tc.get("show_logo")
    use_logo_here = body.show_logo or bool(inherit_logo)
    text_content = SlideTextContent(
        headline=body.headline, accent_keyword=body.accent_keyword, author=body.author,
            icon_name=body.icon_name, illustration_accent=body.illustration_accent,
            show_logo=use_logo_here, show_mascot=first_tc.get("show_mascot"),
    )
    rendered = await _render_and_upload_slide(
        prompt=prompt,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        layout=body.active_layout,
        brand=brand,
        brand_tokens=_brand_tokens_from(brand),
        text_content=text_content,
        show_logo=use_logo_here,
        show_mascot=bool(first_tc.get("show_mascot")),
    )

    next_number = max((s.slide_number for s in asset.slides), default=0) + 1
    new_slide = Slide(
        slide_number=next_number,
        title=body.headline[:80] or asset.title,
        slide_type=body.active_layout.value,
        layout=body.active_layout,
        media_id=rendered.media.id,
        background_media_id=rendered.background_media_id,
        layers=rendered.layers,
        text_content=text_content.model_dump(),
    )
    updated_doc = await _bump_version(
        image_asset_id, ctx.workspace_id, asset.slides + [new_slide], "slide_added", ctx.user_id
    )
    return ImageAsset(**updated_doc)


class SaveLayersRequest(BaseModel):
    layers: list[Layer] = Field(max_length=MAX_LAYERS)


async def _slide_for_editing(image_asset_id: str, slide_number: int, workspace_id: str) -> tuple[ImageAsset, Slide, dict]:
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)
    slide = next((sl for sl in asset.slides if sl.slide_number == slide_number), None)
    if slide is None:
        raise HTTPException(status_code=404, detail="Slide not found.")
    if slide.layout is None:
        raise HTTPException(status_code=400, detail="This picture was uploaded, not designed here, so it has no layers to edit.")
    brand = await _get_brand_profile(asset.brand_id, workspace_id)
    return asset, slide, brand


async def _redraw_slide(asset: ImageAsset, slide: Slide, layers: list[Layer], brand: dict, ctx: WorkspaceContext, action: str) -> ImageAsset:
    """Draws the given layers over the slide's clean picture, stores the result and saves it as a new version. Slides made
    before the editor existed have no clean picture, so their finished picture (words and all) is used as the backdrop."""
    assert slide.layout is not None
    size = LAYOUT_DIMS[slide.layout]
    background = await _media_bytes(slide.background_media_id or slide.media_id, ctx.workspace_id)
    brand_tokens = _brand_tokens_from(brand)
    media = await _draw_and_store(size=size, background_bytes=background, layers=layers, brand=brand, brand_tokens=brand_tokens,
                                  workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    updated = [
        sl.model_copy(update={"media_id": media.id, "layers": layers}) if sl.slide_number == slide.slide_number else sl
        for sl in asset.slides
    ]
    doc = await _bump_version(asset.id, ctx.workspace_id, updated, action, ctx.user_id)
    return ImageAsset(**doc)


@router.put("/{image_asset_id}/slides/{slide_number}/layers", response_model=ImageAsset)
@limiter.limit("30/minute")
async def save_slide_layers(
    request: Request,
    image_asset_id: str,
    slide_number: int,
    body: SaveLayersRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ImageAsset:
    """Saves the member's edits: the picture is drawn again from these layers, over the same clean AI picture, and kept as
    a new version. No AI call is made, so editing is free."""
    asset, slide, brand = await _slide_for_editing(image_asset_id, slide_number, ctx.workspace_id)
    ids = [layer.id for layer in body.layers]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Two layers share the same id.")
    for layer in body.layers:
        if layer.type == "image":
            if not layer.media_id or not await media_assets.find_one({"id": layer.media_id, "workspace_id": ctx.workspace_id, "kind": MediaKind.IMAGE.value}):
                raise HTTPException(status_code=400, detail="An image layer must use a picture from this workspace's library.")
    return await _redraw_slide(asset, slide, body.layers, brand, ctx, "design_edited")


@router.post("/{image_asset_id}/slides/{slide_number}/layers/reset", response_model=ImageAsset)
@limiter.limit("20/minute")
async def reset_slide_layers(
    request: Request,
    image_asset_id: str,
    slide_number: int,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ImageAsset:
    """Goes back to the design the picture started with (headline, band, accent bar, logo), rebuilt from the slide's own
    text settings. Kept as a new version, so the edited design can be restored."""
    asset, slide, brand = await _slide_for_editing(image_asset_id, slide_number, ctx.workspace_id)
    if not slide.background_media_id:
        raise HTTPException(status_code=400, detail="This picture was made before the editor existed, so there is no clean picture to rebuild from.")
    tc = slide.text_content or {}
    visual_identity = brand.get("visual_identity") or {}
    layers = default_layers(
        size=LAYOUT_DIMS[slide.layout], brand=_brand_tokens_from(brand), headline=str(tc.get("headline") or ""), show_text=bool(tc.get("show_text", True)),
        accent_keyword=str(tc.get("accent_keyword") or ""), author=tc.get("author") or None,
        has_logo=bool(visual_identity.get("logo_url")) and tc.get("show_logo") is not False,
        has_mascot=bool(visual_identity.get("mascot_url")) and tc.get("show_mascot") is True,
        icon_name=tc.get("icon_name") or None, illustration_accent=bool(tc.get("illustration_accent", False)),
    )
    return await _redraw_slide(asset, slide, layers, brand, ctx, "design_reset")


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


# ── Comment pins: a real point-on-a-slide review note, not a general note ───
# Embedded on the ImageAsset doc (asset.comments), same as the plan's own
# CommentPin model — mirrors audio_assets.py's comment endpoints' shape,
# but a separate collection isn't needed here since Image never had one.

@router.get("/{image_asset_id}/comments", response_model=list[CommentPin])
async def list_image_comments(
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> list[CommentPin]:
    doc = await image_assets.find_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id}, {"comments": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    return [CommentPin(**c) for c in doc.get("comments", [])]


@router.post("/{image_asset_id}/comments", response_model=CommentPin, status_code=201)
@limiter.limit("60/minute")
async def create_image_comment(
    request: Request,
    image_asset_id: str,
    body: CommentPinCreate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> CommentPin:
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    asset = ImageAsset(**doc)
    if not any(s.slide_number == body.slide_number for s in asset.slides):
        raise HTTPException(status_code=400, detail=f"Slide {body.slide_number} doesn't exist on this asset.")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write something for the note.")
    if len(text) > 1000:
        raise HTTPException(status_code=400, detail="Keep the note under 1000 characters.")
    if not (0 <= body.x <= 100) or not (0 <= body.y <= 100):
        raise HTTPException(status_code=400, detail="The pin position must be within the slide.")

    user = await users.find_one({"id": ctx.user_id}, {"name": 1, "email": 1})
    pin = CommentPin(
        id=uuid4().hex,
        slide_number=body.slide_number,
        author_id=ctx.user_id,
        author_name=((user or {}).get("name") or (user or {}).get("email") or "Member"),
        x=body.x,
        y=body.y,
        text=text,
        created_at=datetime.now(timezone.utc),
    )
    await image_assets.update_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id},
        {"$push": {"comments": pin.model_dump()}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    return pin


@router.patch("/{image_asset_id}/comments/{comment_id}", response_model=CommentPin)
@limiter.limit("60/minute")
async def update_image_comment(
    request: Request,
    image_asset_id: str,
    comment_id: str,
    body: CommentPinUpdate,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> CommentPin:
    result = await image_assets.find_one_and_update(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id, "comments.id": comment_id},
        {"$set": {"comments.$.resolved": body.resolved, "updated_at": datetime.now(timezone.utc)}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Comment not found.")
    updated = next(c for c in result["comments"] if c["id"] == comment_id)
    return CommentPin(**updated)


@router.delete("/{image_asset_id}/comments/{comment_id}", status_code=204)
@limiter.limit("60/minute")
async def delete_image_comment(
    request: Request,
    image_asset_id: str,
    comment_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> Response:
    doc = await image_assets.find_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id, "comments.id": comment_id}, {"comments": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Comment not found.")
    pin = next(c for c in doc["comments"] if c["id"] == comment_id)
    if pin["author_id"] != ctx.user_id and ctx.role not in (WorkspaceRole.OWNER.value, WorkspaceRole.ADMIN.value):
        raise HTTPException(status_code=403, detail="Only the author, or a workspace owner/admin, can delete this note.")
    await image_assets.update_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id},
        {"$pull": {"comments": {"id": comment_id}}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )
    return Response(status_code=204)


@router.get("/{image_asset_id}/contrast-check", response_model=list[ContrastResult])
async def check_image_contrast(
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> list[ContrastResult]:
    """Real WCAG contrast check on the exact color pairs render_slide()
    draws for this asset's brand — not a guessed or generic pair."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    brand = await brand_profiles.find_one({"id": doc["brand_id"], "workspace_id": ctx.workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="This asset's brand no longer exists.")
    tokens = _brand_tokens_from(brand)
    return check_slide_contrast(_DEFAULT_FG, tokens.accent_hex, tokens.primary_hex)


@router.get("/")
@limiter.limit("60/minute")
async def list_image_assets(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> dict:
    """The workspace's images, newest first, each with its first slide as the
    preview, for the Library's Image tab. Read only."""
    flt = {"workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}
    docs = await image_assets.find(flt, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    total = await image_assets.count_documents(flt)

    def _first_media_id(doc: dict) -> Optional[str]:
        for slide in doc.get("slides") or []:
            if slide.get("media_id"):
                return slide["media_id"]
        return None

    media_ids = [m for m in (_first_media_id(d) for d in docs) if m]
    media_by_id: dict = {}
    if media_ids:
        found = await media_assets.find(
            {"id": {"$in": media_ids}, "workspace_id": ctx.workspace_id}, {"_id": 0}
        ).to_list(length=len(media_ids))
        media_by_id = {m["id"]: m for m in found}

    items = []
    for d in docs:
        media_id = _first_media_id(d)
        items.append({
            "id": d["id"],
            "title": d.get("title", ""),
            "source_type": d.get("source_type"),
            "created_at": d.get("created_at"),
            "approval_status": d.get("approval_status"),
            "slide_count": len(d.get("slides") or []),
            "brand_id": d.get("brand_id"),
            "media": media_by_id.get(media_id) if media_id else None,
        })
    return {"items": items, "total": total}


@router.get("/{image_asset_id}", response_model=ImageAsset)
@limiter.limit("120/minute")
async def get_image_asset(
    request: Request,
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> ImageAsset:
    """One image project with everything the Image pipeline needs to open it again: prompt, avoid list, headline settings,
    every slide and its layers. Used by "Open in Image pipeline" and the History tab."""
    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    return ImageAsset(**doc)


class RenameRequest(BaseModel):
    title: str


@router.patch("/{image_asset_id}/rename")
@limiter.limit("30/minute")
async def rename_image_asset(
    request: Request,
    image_asset_id: str,
    body: RenameRequest,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    from app.shared.titles import clean_title

    title = clean_title(body.title)
    done = await image_assets.update_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}},
    )
    if done.matched_count == 0:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    return {"id": image_asset_id, "title": title}


@router.delete("/{image_asset_id}")
@limiter.limit("20/minute")
async def remove_image_asset(
    request: Request,
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(require("edit_content")),
) -> dict:
    """Remove a picture project from History and the Library. Posts that already use its pictures keep them."""
    done = await image_assets.update_one(
        {"id": image_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}},
        {"$set": {"deleted": True, "deleted_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc)}},
    )
    if done.matched_count == 0:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    return {"id": image_asset_id, "deleted": True}


class ImageDraftRequest(BaseModel):
    # A post platform name ("Instagram", "LinkedIn"...). Left empty, Instagram.
    platform: Optional[str] = None
    # The post text. Left empty, the project's name is used.
    caption: Optional[str] = None
    # Which slide to attach. Left empty, the first one with a picture.
    slide_number: Optional[int] = None


@router.post("/{image_asset_id}/send-to-draft")
@limiter.limit("20/minute")
async def send_image_to_draft(
    request: Request,
    image_asset_id: str,
    body: ImageDraftRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> dict:
    """Make a draft post from this picture project, with the picture attached, in one step. Safe to repeat: the same project and platform
    always give back the same draft."""
    from app.models.text import Platform
    from app.pipelines.publish.asset_drafts import draft_from_asset

    doc = await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id, "deleted": {"$ne": True}})
    if not doc:
        raise HTTPException(status_code=404, detail="Image asset not found.")
    if doc.get("approval_status") != ImageApprovalStatus.APPROVED.value:
        raise HTTPException(status_code=409, detail="Approve this picture before it can be posted.")
    try:
        platform = Platform((body.platform or "").strip() or "Instagram").value
    except ValueError:
        raise HTTPException(status_code=400, detail="That platform isn't supported for posts.")
    caption = (body.caption or "").strip() or (doc.get("title") or "").strip()
    if not caption:
        raise HTTPException(status_code=400, detail="Add some text for the post.")
    return await draft_from_asset(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, brand_id=doc["brand_id"], platform=platform, caption=caption,
        origin={"image_asset_id": image_asset_id, "platform": platform}, group_by=image_asset_id,
        attach={"asset_type": "image", "asset_id": image_asset_id, "slide_number": body.slide_number},
    )


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


class ShareLinkItem(BaseModel):
    token: str
    url: str
    created_at: datetime
    expires_at: datetime


@router.get("/{image_asset_id}/share-links", response_model=list[ShareLinkItem])
async def list_image_share_links(
    image_asset_id: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> list[ShareLinkItem]:
    """This image's links that still work — not revoked and not expired."""
    if not await image_assets.find_one({"id": image_asset_id, "workspace_id": ctx.workspace_id}, {"id": 1}):
        raise HTTPException(status_code=404, detail="Image asset not found.")
    now = datetime.now(timezone.utc)
    docs = await image_share_links.find(
        {"image_asset_id": image_asset_id, "workspace_id": ctx.workspace_id, "revoked": False},
    ).sort("created_at", -1).to_list(length=100)
    items = []
    for d in docs:
        expires = d["expires_at"] if d["expires_at"].tzinfo else d["expires_at"].replace(tzinfo=timezone.utc)
        if expires <= now:
            continue
        items.append(ShareLinkItem(
            token=d["token"], url=f"{settings.FRONTEND_URL}/share/{d['token']}",
            created_at=d["created_at"], expires_at=d["expires_at"],
        ))
    return items


@router.delete("/{image_asset_id}/share-link/{token}", status_code=204)
async def revoke_image_share_link(
    image_asset_id: str,
    token: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> Response:
    """Stops a link working immediately; the record stays (revoked)."""
    result = await image_share_links.update_one(
        {"token": token, "image_asset_id": image_asset_id, "workspace_id": ctx.workspace_id},
        {"$set": {"revoked": True}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Share link not found.")
    return Response(status_code=204)
