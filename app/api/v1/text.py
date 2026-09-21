"""Text pipeline API routes — generate, repurpose, batch, score, refine.

Workspace-scoped: brand + content are resolved within the caller's active
workspace (``X-Workspace-Id`` header or default). Generation / refinement
require the ``create_content`` permission; scoring and chip listing require
membership only.
"""

import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import limiter
from app.core.workspace import WorkspaceContext, get_current_workspace, require
from app.db.mongo import brand_profiles
from app.models.text import (
    BatchGenerateRequest,
    GenerateTextRequest,
    PreviewUrlRequest,
    PreviewUrlResponse,
    RepurposeRequest,
    TextPipelineResult,
    ContentIntent,
    RegenerateRequest,
    RegenerateResponse,
    InputSourceType,
    AgentTask,
    GenerateAnglesRequest,
    GenerateAnglesResponse,
    SuggestRepurposeRequest,
    RepurposeSuggestion,

)
from app.models.scorer import (
    ScoreHookRequest,
    ScoreHookResponse,
    ScoreReadabilityRequest,
    ScoreReadabilityResponse,
)
from app.models.chips import ApplyChipRequest, ApplyChipResponse, GetChipsResponse
from app.shared.language import detect_language, first_present_or_none, user_language, workspace_language
from app.pipelines.text.orchestrator import run_batch_pipeline, run_text_pipeline
from app.pipelines.text.scraper import preview_url, scrape_url
from app.pipelines.text.scorer import score_hook, score_readability
from app.pipelines.text.brand_context import build_brand_context
from app.pipelines.text.chips import apply_chip, get_chips_for_platform, CHIP_PROMPTS
from app.pipelines.text.angles import run_angles_agent
from app.pipelines.text.repurpose_suggest import suggest_repurpose_targets
from app.pipelines.text.storage import save_pipeline_result, update_piece_content
from app.agents.text.nodes import _extract_enforcement_data
from app.models.refiner import RefineChatRequest, RefineChatResponse
from app.pipelines.text.refiner import run_refinement_turn

router = APIRouter()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _get_verified_brand(brand_id: str, workspace_id: str) -> dict:
    """Fetch a brand profile in the workspace and verify it is complete."""
    brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    if not brand.get("is_complete"):
        raise HTTPException(
            status_code=400,
            detail="Brand profile is not complete. Finish onboarding first.",
        )
    return brand


async def _get_owned_brand(brand_id: str, workspace_id: str) -> dict:
    """Fetch a brand profile in the workspace (no completeness requirement)."""
    brand = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id})
    if not brand:
        raise HTTPException(status_code=404, detail="Brand profile not found.")
    return brand


async def _resolve_request_language(
    request_language: str | None,
    ctx: WorkspaceContext,
    content_for_detection: str | None = None,
) -> str:
    """The precedence chain for this request's content language.

    request override > workspace default > caller's own account default >
    detected from the request's own source content > "en". See
    app.shared.language for why generation uses this order (the workspace's
    audience, not the clicking staff member, is what matters) — different
    from Remy's or Odette's own chains, and for why detection only fires
    once every explicit-preference tier above it has come back empty (a
    Stage-5 addition: py3langid, gated at MIN_DETECTION_CONFIDENCE so a
    low-signal or mixed-script guess falls through to "en" rather than
    asserting a wrong language).
    """
    explicit = first_present_or_none(
        request_language,
        await workspace_language(ctx.workspace_id),
        await user_language(ctx.user_id),
    )
    if explicit:
        return explicit
    if content_for_detection:
        detected = detect_language(content_for_detection)
        if detected:
            return detected
    return "en"


async def _save_result(
    result: TextPipelineResult,
    goal: Any = None,
    tone: Any = None,
    is_repurpose: bool = False,
    label: str = "result",
) -> list[str]:
    """
    Save pipeline result to MongoDB.
    Non-blocking — never fails the API response.
    Storage failure is logged but does not raise.

    Returns the real, persisted piece_ids in the same order as
    result.pieces — [] on a storage failure. save_pipeline_result()
    generates each piece_id itself (GeneratedPiece has no piece_id field
    of its own), so this is the only place that id is ever knowable;
    callers that need to hand a real piece_id back to the frontend (see
    regenerate_content below) must read it from here, not from `result`.
    """
    try:
        session_id, piece_ids = await save_pipeline_result(
            result=result,
            goal=goal.value if hasattr(goal, "value") else goal,
            tone=tone.value if hasattr(tone, "value") else tone,
            is_repurpose=is_repurpose,
        )
        logger.info(
            "Saved %s — session %s, %d pieces",
            label, session_id, len(piece_ids),
        )
        return piece_ids
    except Exception as e:
        logger.error("Failed to save %s to storage: %s", label, e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# URL PREVIEW
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/preview-url", response_model=PreviewUrlResponse)
@limiter.limit("20/minute")
async def preview_url_content(
    request: Request,
    body: PreviewUrlRequest,
    ctx: WorkspaceContext = Depends(get_current_workspace),
) -> PreviewUrlResponse:
    """
    Fetch a URL and return a lightweight preview (title/snippet/word count)
    for the URL input tab's "Fetch" button — before this, that button showed
    a hardcoded fake preview card for literally any URL typed in.
    """
    try:
        result = await preview_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return PreviewUrlResponse(**result)


# ─────────────────────────────────────────────────────────────────────────────
# GENERATE
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/generate", response_model=TextPipelineResult)
@limiter.limit("20/minute")
async def generate_text_content(
    request: Request,
    body: GenerateTextRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> TextPipelineResult:
    """
    Generate content for one or more platforms from any input.
    Supports all four frontend input modes: write, prompt, url, repurpose.
    """
    await _get_verified_brand(body.brand_id, ctx.workspace_id)
    language = await _resolve_request_language(body.language, ctx, content_for_detection=body.content)

    if body.batch_mode:
        try:
            results = await run_batch_pipeline(
                topic_cluster=body.content,
                platforms=body.platforms,
                brand_id=body.brand_id,
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
                extras=body.extras,
                days=body.batch_days,
                detected_intent=body.detected_intent,
                language=language,
            )
            # Save all batch day results
            for i, day_result in enumerate(results):
                await _save_result(
                    day_result,
                    goal=body.goal,
                    tone=body.tone,
                    label=f"batch day {i + 1}",
                )
            return results[0] if results else TextPipelineResult(
                session_id="empty",
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
                brand_id=body.brand_id,
                pieces=[],
                source_type=body.source_type,
                created_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Batch generation error: {str(e)}")

    try:
        result = await run_text_pipeline(
            source_type=body.source_type,
            content=body.content,
            platforms=body.platforms,
            brand_id=body.brand_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            extras=body.extras,
            goal=body.goal,
            tone=body.tone,
            intent=body.intent,
            language=language,
            schedule_mode=body.schedule_mode.value,
            scheduled_at=body.scheduled_at,
            publish_targets=body.publish_targets,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")

    # The graph's collect_output_node already persisted every piece for real
    # (ensure_session_exists + save_live_piece, live, per platform) and
    # stamped its real piece_id onto it — save_pipeline_result() used to run
    # again unconditionally here and re-insert a session document with the
    # same session_id that call just upserted, which MongoDB's unique index
    # on content_sessions.session_id rejects (E11000). Only fall back to it
    # if live persistence left every piece without an id (e.g. it failed
    # outright for every platform), so there's still a real save either way.
    if not any(p.piece_id for p in result.pieces):
        piece_ids = await _save_result(result, goal=body.goal, tone=body.tone, label="generate")
        for piece, piece_id in zip(result.pieces, piece_ids):
            piece.piece_id = piece_id

    return result


# ─────────────────────────────────────────────────────────────────────────────
# REPURPOSE
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/repurpose", response_model=TextPipelineResult)
@limiter.limit("20/minute")
async def repurpose_content(
    request: Request,
    body: RepurposeRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> TextPipelineResult:
    """
    Repurpose existing content from one platform format to others.
    Brand voice is always re-applied — never copy-paste.
    """
    await _get_verified_brand(body.brand_id, ctx.workspace_id)
    language = await _resolve_request_language(body.language, ctx, content_for_detection=body.source_content)

    try:
        result = await run_text_pipeline(
            source_type=body.source_type,
            content=body.source_content,
            platforms=body.target_platforms,
            brand_id=body.brand_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            extras=body.extras,
            goal=body.goal,
            tone=body.tone,
            source_platform=body.source_platform,
            is_repurpose=True,
            intent=ContentIntent.AUTO,
            language=language,
            structure_rules=(
                [r.model_dump() for r in body.structure_rules] if body.structure_rules else None
            ),
        )
    except ValueError as e:
        # Bad input, not a server failure — an unscrapable/JS-gated/paywalled
        # URL (scrape_url's ValueError) is the common case reported by
        # users. Same distinction generate_text_content() already makes;
        # this endpoint was folding it into a generic 500 "Repurpose error"
        # instead, which reads as "something broke" rather than "try a
        # different URL or paste the content directly".
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Repurpose error: {str(e)}")

    # _run_single_repurpose already persisted every piece for real (live,
    # per platform) and stamped its real piece_id onto it — see
    # generate_text_content()'s identical comment for why calling
    # save_pipeline_result() again unconditionally here used to crash into
    # MongoDB's unique index on content_sessions.session_id.
    if not any(p.piece_id for p in result.pieces):
        piece_ids = await _save_result(
            result,
            goal=body.goal,
            tone=body.tone,
            is_repurpose=True,
            label="repurpose",
        )
        for piece, piece_id in zip(result.pieces, piece_ids):
            piece.piece_id = piece_id

    return result


@router.post("/repurpose/suggest", response_model=RepurposeSuggestion)
@limiter.limit("20/minute")
async def suggest_repurpose(
    request: Request,
    body: SuggestRepurposeRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> RepurposeSuggestion:
    """
    New repurpose flow's "AI Suggestions" step — a cheap, read-only call
    suggesting which platforms best fit this content (and a tone/angle
    note) before the user commits to a full /repurpose generation. Never
    persists anything; a suggestion failure never blocks manual picking.
    """
    brand_profile = await _get_owned_brand(body.brand_id, ctx.workspace_id)
    brand_context = build_brand_context(brand_profile)

    content = body.source_content
    if body.source_type == InputSourceType.URL:
        try:
            content = await scrape_url(body.source_content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    result = await suggest_repurpose_targets(
        content=content,
        source_platform=body.source_platform,
        brand_context=brand_context,
    )
    return RepurposeSuggestion(**result)


# ─────────────────────────────────────────────────────────────────────────────
# BATCH
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/batch", response_model=list[TextPipelineResult])
@limiter.limit("5/minute")
async def batch_generate(
    request: Request,
    body: BatchGenerateRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> list[TextPipelineResult]:
    """
    Generate a full week of content from a single topic cluster.
    Rate limited to 5/minute — expensive operation.
    """
    await _get_verified_brand(body.brand_id, ctx.workspace_id)
    language = await _resolve_request_language(body.language, ctx, content_for_detection=body.topic_cluster)

    try:
        results = await run_batch_pipeline(
            topic_cluster=body.topic_cluster,
            platforms=body.platforms,
            brand_id=body.brand_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            extras=body.extras,
            days=body.days,
            language=language,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch error: {str(e)}")

    # Save each day — non-blocking
    for i, day_result in enumerate(results):
        await _save_result(
            day_result,
            goal=None,
            tone=None,
            label=f"batch day {i + 1}",
        )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# HOOK SCORER
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/score-hook", response_model=ScoreHookResponse)
@limiter.limit("30/minute")
async def score_hook_endpoint(
    request: Request,
    body: ScoreHookRequest,
    # LLM-backed (Groq) — used to require only membership, so a viewer could
    # trigger real LLM calls with no gate at all. create_content matches the
    # other content-generation-adjacent actions (refine, refine-chat, chips).
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ScoreHookResponse:
    """
    Score the hook quality of existing content.
    Returns current score, weakness diagnosis, and 3 scored alternatives.
    """
    brand_profile = await _get_owned_brand(body.brand_id, ctx.workspace_id)

    brand_context = build_brand_context(brand_profile)
    enforcement = _extract_enforcement_data(brand_profile)

    result = await score_hook(
        content=body.content,
        platform=body.platform,
        brand_context=brand_context,
        approved_openers=enforcement.get("approved_openers", []),
        banned_words=enforcement.get("banned_words", []),
        session_id=str(uuid4()),
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return ScoreHookResponse(**result)


# ─────────────────────────────────────────────────────────────────────────────
# READABILITY SCORER
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/score-readability", response_model=ScoreReadabilityResponse)
@limiter.limit("60/minute")
async def score_readability_endpoint(
    request: Request,
    body: ScoreReadabilityRequest,
    # Pure computation, no LLM cost — gated the same as score-hook anyway,
    # for consistency with the rest of this create-content-adjacent group
    # rather than because this specific one is expensive.
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ScoreReadabilityResponse:
    """
    Score the readability of content for a specific platform.
    Pure computation — no LLM call.
    """
    result = score_readability(body.content, body.platform)
    return ScoreReadabilityResponse(**result)


# ─────────────────────────────────────────────────────────────────────────────
# QUICK ACTION CHIPS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/refine", response_model=ApplyChipResponse)
@limiter.limit("30/minute")
async def refine_content(
    request: Request,
    body: ApplyChipRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> ApplyChipResponse:
    """
    Apply a quick action chip to existing content.
    If piece_id provided, saves a new version to version history.
    Brand voice and banned words enforced on output.
    """
    # Validate chip exists before hitting the DB — skipped for a custom,
    # user-authored instruction (Feature 5), which isn't in CHIP_PROMPTS by
    # definition.
    if not body.custom_instruction and body.chip not in CHIP_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown chip '{body.chip}'. Available: {', '.join(CHIP_PROMPTS.keys())}",
        )

    brand_profile = await _get_owned_brand(body.brand_id, ctx.workspace_id)

    brand_context = build_brand_context(brand_profile)
    enforcement = _extract_enforcement_data(brand_profile)
    language = await _resolve_request_language(None, ctx, content_for_detection=body.content)

    result = await apply_chip(
        content=body.content,
        chip_name=body.chip,
        platform=body.platform,
        brand_context=brand_context,
        banned_words=enforcement.get("banned_words", []),
        custom_instruction=body.custom_instruction,
        language=language,
        default_tone=brand_profile.get("default_tone"),
    )

    # Save version if piece_id provided and content changed
    if body.piece_id and result.get("changed"):
        try:
            await update_piece_content(
                piece_id=body.piece_id,
                workspace_id=ctx.workspace_id,
                new_content=result["refined"],
                action=body.chip,
                instruction=body.custom_instruction or CHIP_PROMPTS.get(body.chip, body.chip),
            )
            logger.info(
                "Version saved for piece %s via chip %s",
                body.piece_id, body.chip,
            )
        except Exception as e:
            # Non-fatal — chip result still returned even if version save fails
            logger.error(
                "Failed to save version for piece %s: %s",
                body.piece_id, e,
            )

    return ApplyChipResponse(**result)


# ─────────────────────────────────────────────────────────────────────────────
# ANGLES — Feature 8's real "3 Fresh Angles"
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/angles", response_model=GenerateAnglesResponse)
@limiter.limit("15/minute")
async def generate_angles(
    request: Request,
    body: GenerateAnglesRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> GenerateAnglesResponse:
    """
    Generate 3 genuinely distinct strategic angles on existing content —
    not 3 tone variations, 3 different choices of what to lead with.
    Read-only: does not persist anything. The frontend calls PATCH
    /content/pieces/{id} separately once the user picks one to keep.
    """
    brand_profile = await _get_owned_brand(body.brand_id, ctx.workspace_id)
    brand_context = build_brand_context(brand_profile)
    enforcement = _extract_enforcement_data(brand_profile)

    result = await run_angles_agent(
        AgentTask(
            agent="angles",
            platform=body.platform,
            content=body.content,
            brand_context=brand_context,
            session_id=body.piece_id or "angles-preview",
            metadata={"banned_words": enforcement.get("banned_words", [])},
        )
    )

    if not result.success:
        raise HTTPException(
            status_code=502,
            detail="Couldn't generate angle variants — please try again.",
        )

    return GenerateAnglesResponse(angles=result.output["angles"])


@router.post("/refine-chat", response_model=RefineChatResponse)
@limiter.limit("20/minute")
async def refine_chat(
    request: Request,
    body: RefineChatRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> RefineChatResponse:
    """
    Multi-turn conversational content refinement.

    Send the full conversation history on every request.
    Each turn refines the previous version based on the user instruction.
    Brand voice enforced on every turn.

    If piece_id is provided and content changed, saves a new version
    to version history automatically.
    """
    brand_profile = await _get_owned_brand(body.brand_id, ctx.workspace_id)

    brand_context = build_brand_context(brand_profile)
    enforcement = _extract_enforcement_data(brand_profile)

    # Convert Pydantic models to plain dicts for llm.py
    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    # Validate message roles
    for msg in messages:
        if msg["role"] not in ("user", "assistant"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid message role '{msg['role']}'. Must be 'user' or 'assistant'.",
            )

    # Last message must be from user
    if messages[-1]["role"] != "user":
        raise HTTPException(
            status_code=400,
            detail="Last message must be from the user.",
        )

    language = await _resolve_request_language(None, ctx, content_for_detection=messages[-1]["content"])

    refined = await run_refinement_turn(
        messages=messages,
        brand_context=brand_context,
        platform=body.platform,
        banned_words=enforcement.get("banned_words", []),
        language=language,
        default_tone=brand_profile.get("default_tone"),
    )

    refined = refined.strip()
    turn = sum(1 for m in messages if m["role"] == "assistant") + 1

    # Save version if piece_id provided
    version_saved = False
    if body.piece_id and refined:
        try:
            await update_piece_content(
                piece_id=body.piece_id,
                workspace_id=ctx.workspace_id,
                new_content=refined,
                action=f"chat_turn_{turn}",
                instruction=messages[-1]["content"][:200],
            )
            version_saved = True
            logger.info(
                "Chat turn %d version saved for piece %s",
                turn, body.piece_id,
            )
        except Exception as e:
            logger.error(
                "Failed to save chat version for piece %s: %s",
                body.piece_id, e,
            )

    return RefineChatResponse(
        refined=refined,
        platform=body.platform,
        word_count=len(refined.split()),
        char_count=len(refined),
        turn=turn,
        piece_id=body.piece_id,
        version_saved=version_saved,
    )

@router.get("/chips", response_model=GetChipsResponse)
@limiter.limit("100/minute")
async def get_chips(
    request: Request,
    platform: str,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> GetChipsResponse:
    """
    Get available chip names for a platform.
    Frontend uses this to render chip buttons.
    """
    chips = get_chips_for_platform(platform)
    return GetChipsResponse(
        platform=platform,
        chips=chips,
        total=len(chips),
    )

@router.post("/regenerate", response_model=RegenerateResponse)
@limiter.limit("20/minute")
async def regenerate_content(
    request: Request,
    body: RegenerateRequest,
    ctx: WorkspaceContext = Depends(require("create_content")),
) -> RegenerateResponse:
    """
    Regenerate content for a single platform from scratch.

    Resolution order for source content:
      1. body.content if provided directly (fastest path)
      2. raw_input on the parent session fetched via piece_id
      3. piece content itself as final fallback

    Runs the full LangGraph pipeline for just the one platform.
    Brand voice, tone, and goal are re-applied identically to the original run.
    """
    await _get_verified_brand(body.brand_id, ctx.workspace_id)

    # 1. Resolve source content
    source_content: str = body.content or ""

    if body.piece_id and not source_content:
        try:
            from app.pipelines.text.storage import get_piece, get_session
            piece_doc = await get_piece(body.piece_id, ctx.workspace_id)
            if piece_doc:
                # Prefer the original raw_input stored on the session document.
                session_doc = await get_session(piece_doc["session_id"], ctx.workspace_id)
                source_content = (
                    (session_doc or {}).get("raw_input")
                    or piece_doc.get("content")
                    or ""
                )
        except Exception as e:
            logger.warning(
                "Could not fetch piece %s for regenerate, using empty content: %s",
                body.piece_id, e,
            )

    if not source_content:
        raise HTTPException(
            status_code=400,
            detail="No source content available. Provide content or a valid piece_id.",
        )

    # 2. Resolve platform enum
    try:
        from app.models.text import Platform
        platform_enum = Platform(body.platform)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown platform '{body.platform}'.",
        )

    from app.models.text import ToneOverride, ContentGoal, InputSourceType

    try:
        tone_enum = ToneOverride(body.tone) if body.tone else ToneOverride.BRAND
    except ValueError:
        tone_enum = ToneOverride.BRAND

    try:
        goal_enum = ContentGoal(body.goal) if body.goal else None
    except ValueError:
        goal_enum = None

    # 4. Minimal extras object (regenerate always uses brand defaults)
    class _MinimalExtras:
        hook_variations  = True
        hashtags         = True
        auto_cta         = False
        seo_meta         = False
        grammar_check    = False
        plagiarism_check = False
        avoid_blacklist  = True
        pdf_export       = False

    # 5. Resolve language — same precedence chain /generate, /repurpose, and
    # /batch already use (request override > workspace > user > detected
    # from source content > "en"). Regenerate never called this before, so
    # it silently fell through to run_text_pipeline's own "en" default
    # regardless of the workspace/brand's real language.
    language = await _resolve_request_language(None, ctx, content_for_detection=source_content)

    # 6. Run pipeline for the single platform
    try:
        result = await run_text_pipeline(
            source_type=InputSourceType.TEXT,
            content=source_content,
            platforms=[platform_enum],
            brand_id=body.brand_id,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            extras=_MinimalExtras(),
            goal=goal_enum,
            tone=tone_enum,
            language=language,
            session_id=str(uuid4()),
        )
    except Exception as e:
        logger.error(
            "Regenerate pipeline failed for %s: %s", body.platform, e, exc_info=True
        )
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {str(e)}")

    if not result.pieces:
        raise HTTPException(status_code=500, detail="Regeneration produced no output.")

    piece = result.pieces[0]

    # 6. Persist regenerated content. Product decision: regenerating an
    # existing piece creates a new VERSION of that same piece — the same
    # mechanism chips/chat-refine/manual-edit all already use — not a
    # disconnected second piece with no link back to the card the user
    # clicked regenerate on. Only falls back to creating a brand-new piece
    # when there's genuinely nothing to version onto (no piece_id given, or
    # the given one didn't resolve — e.g. wrong workspace, already deleted).
    real_piece_id = ""
    if body.piece_id:
        updated = await update_piece_content(
            piece_id=body.piece_id,
            workspace_id=ctx.workspace_id,
            new_content=piece.content,
            action="regenerated",
            instruction="Regenerated from scratch",
        )
        if updated:
            real_piece_id = body.piece_id
        else:
            logger.warning(
                "Regenerate: piece_id %s did not resolve to a real piece in "
                "this workspace — falling back to creating a new one.",
                body.piece_id,
            )

    if not real_piece_id:
        # The real piece_id only ever exists as _save_result's return value;
        # GeneratedPiece itself has no piece_id field, so reading one off
        # `piece` (as this used to do unconditionally) always produced "",
        # even though the piece really was saved. Same root cause Stage 1
        # fixed for the SSE path, independently present here too.
        piece_ids = await _save_result(result, goal=goal_enum, tone=tone_enum, label="regenerate")
        real_piece_id = piece_ids[0] if piece_ids else ""

    # 7. Build response
    hooks = piece.hooks or []
    hook_score = 0
    hook_alternatives: list[str] = []

    if hooks:
        first = hooks[0]
        hook_score = first.get("score", 0) if isinstance(first, dict) else 0
        hook_alternatives = [
            h.get("hook", "") for h in hooks
            if isinstance(h, dict) and h.get("hook")
        ]

    seo = piece.seo or {}
    return RegenerateResponse(
        platform=body.platform,
        content=piece.content,
        hook_score=hook_score,
        readability_score=int(piece.readability_score or 0),
        readability_level=getattr(piece, "readability_level", "Standard") or "Standard",
        piece_id=real_piece_id,
        hashtags=list(seo.get("hashtags", []) or []),
        hook_alternatives=hook_alternatives,
    )
