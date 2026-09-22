"""
Platform registry — single source of truth for "what is a platform" across
Recast, replacing the four independent, drifting sources audited in
docs/PLATFORM_REGISTRY_PLAN.md Stage 0 (the text Platform enum, the publish
registry, the validators dict, the analytics fetchers dict).

Two ways to register a definition:

    @register_platform                              # code-driven, one file per platform
    def _linkedin() -> PlatformDefinition:
        return PlatformDefinition(key="linkedin", ...)

    register_platform(PlatformDefinition(key="mastodon", ...))  # metadata-only bulk entries

Every module under app/platforms/ (and app/platforms/planned/) registers itself
as a side effect of being imported. import_all() imports every one of them —
call it once at startup before anything reads PLATFORM_REGISTRY.
"""

import importlib
import pkgutil
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

Pipeline = Literal["text", "image", "video", "audio"]

# The four patterns docs/PLATFORM_REGISTRY_PLAN.md's source directory groups every
# platform into, plus "generation_only" for the two pre-existing text-generation
# targets (Blog, Newsletter) that were never publish destinations in the first
# place — no publisher ever existed for them, this just names what's real.
IntegrationPattern = Literal[
    "api_publish", "token_webhook", "rss_pull", "manual_handoff", "generation_only"
]

# code_driven = needs a custom Python client (OAuth, SDK calls) — api_publish platforms.
# config_driven = the generic Stage-2 WebhookPublisher/ManualHandoffPublisher/RSS
# path consumes admin-entered config, no per-platform code required.
Mode = Literal["code_driven", "config_driven"]

# active   = real publisher + validator, live in production today (the 5 real platforms).
# partial  = some capability is real (e.g. YouTube's analytics fetcher exists and is
#            used today) but publishing isn't built.
# planned  = metadata only, nothing built.
Status = Literal["active", "partial", "planned"]

Confidence = Literal["verified", "third_party", "unverified"]


class PlatformDefinition(BaseModel):
    key: str                             # stable id, e.g. "linkedin" — matches workspace_connections.platform
    label: str                           # display name, e.g. "LinkedIn"
    category: str                        # source directory's section, e.g. "Social and feed networks"

    pipelines: frozenset[Pipeline] = frozenset()
    # Per-pipeline output shape: "native" | "card" | "audiogram" | "link" | "embedded"
    # | "manual" | "thumbnail" | "description" | "photo" | "attachment" | "show_notes" | "cover_art"
    native_formats: dict[Pipeline, str] = Field(default_factory=dict)
    # Content shapes this platform supports beyond a single post, e.g. twitter's
    # ["post", "thread"] — this is what "fold TWITTER_THREAD into a shape-flag on
    # TWITTER" resolves to at the registry level (see app/platforms/__init__.py
    # module docstring for why the generator.py call sites are untouched for now).
    shapes: list[str] = Field(default_factory=lambda: ["post"])

    mode: Mode
    integration_pattern: IntegrationPattern
    status: Status = "planned"

    # Dotted import paths, resolved lazily via resolve() — never imported at
    # registration time, so registering 71 planned platforms costs nothing and
    # importing this module never risks a circular import with the pipelines it
    # points at.
    publisher_cls: Optional[str] = None
    analytics_fetcher_cls: Optional[str] = None
    validator_fn: Optional[str] = None

    audit_required: bool = False
    rate_limits: Optional[str] = None          # free text — values are too heterogeneous for a typed field
    policy_constraints: list[str] = Field(default_factory=list)
    tone_profile: Optional[str] = None
    access_notes: str = ""
    confidence: Confidence = "unverified"

    # Drives app/models/text.py's derived Platform enum. True only for the
    # platforms that actually have prompt fragments under
    # app/prompts/text/generate/{platform_rules,hashtag_rules,cta_rules}/ today —
    # NOT the same set as "text" in pipelines, which describes what the source
    # platform directory says is conceptually possible, not what's wired into
    # the generator. Conflating the two would silently add ~50 new, unsupported
    # Platform values (every planned platform that lists text as a pipeline)
    # and break PLATFORM_RULES[...] lookups the moment one was selected.
    has_text_prompt_rules: bool = False
    # The exact legacy enum string value (e.g. "LinkedIn", "Twitter/X") — kept
    # separate from `label` so the derived enum's values never drift from what
    # 3+ years of stored content_pieces.platform data and prompt-rule dict keys
    # already depend on, even if `label` is later tuned for display purposes.
    text_enum_value: Optional[str] = None
    # Set only when "thread" is in `shapes` and a legacy `<KEY>_THREAD` enum
    # member exists for it (today: only Twitter/X Thread). See
    # app/platforms/__init__.py's docstring for why this shape stays a
    # registry-level flag instead of a real migration of the enum-keyed
    # generator dicts.
    thread_enum_value: Optional[str] = None

    def resolve_publisher_cls(self):
        return _resolve(self.publisher_cls)

    def resolve_analytics_fetcher_cls(self):
        return _resolve(self.analytics_fetcher_cls)

    def resolve_validator_fn(self):
        return _resolve(self.validator_fn)


def _resolve(dotted_path: Optional[str]):
    if not dotted_path:
        return None
    module_path, _, attr = dotted_path.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


PLATFORM_REGISTRY: dict[str, PlatformDefinition] = {}


def _add(definition: PlatformDefinition) -> None:
    if definition.key in PLATFORM_REGISTRY:
        raise ValueError(f"Platform key '{definition.key}' is already registered")
    PLATFORM_REGISTRY[definition.key] = definition


def register_platform(target):
    """
    Decorator form (one file, one platform):
        @register_platform
        def _linkedin() -> PlatformDefinition:
            return PlatformDefinition(...)

    Direct call form (bulk metadata-only registration):
        register_platform(PlatformDefinition(...))
    """
    if isinstance(target, PlatformDefinition):
        _add(target)
        return target
    if callable(target):
        definition = target()
        if not isinstance(definition, PlatformDefinition):
            raise TypeError(
                f"{target!r} decorated with @register_platform must return a PlatformDefinition"
            )
        _add(definition)
        return target
    raise TypeError("register_platform expects a PlatformDefinition or a zero-arg factory")


def get_platform(key: str) -> Optional[PlatformDefinition]:
    return PLATFORM_REGISTRY.get(key.lower())


def resolve_platform_by_display_value(value: str) -> Optional[PlatformDefinition]:
    """Reverse lookup: a display string like "LinkedIn" or "Twitter/X Thread"
    (workspace_connections/content_pieces/events all store platforms this way
    in places, not by registry key) back to its PlatformDefinition. Checks
    key, label, text_enum_value and thread_enum_value, case-sensitively first
    (exact match is unambiguous) then case-insensitively as a fallback."""
    if not value:
        return None
    for definition in PLATFORM_REGISTRY.values():
        if value in (definition.key, definition.label, definition.text_enum_value, definition.thread_enum_value):
            return definition
    lowered = value.lower()
    for definition in PLATFORM_REGISTRY.values():
        candidates = (definition.key, definition.label, definition.text_enum_value, definition.thread_enum_value)
        if lowered in (c.lower() for c in candidates if c):
            return definition
    return None


def list_platforms(
    status: Optional[Status] = None,
    pipeline: Optional[Pipeline] = None,
) -> list[PlatformDefinition]:
    values = list(PLATFORM_REGISTRY.values())
    if status is not None:
        values = [p for p in values if p.status == status]
    if pipeline is not None:
        values = [p for p in values if pipeline in p.pipelines]
    return sorted(values, key=lambda p: (p.category, p.label))


def build_text_platform_enum(enum_name: str = "Platform"):
    """Build a str Enum from every registered platform with
    has_text_prompt_rules=True, using each one's text_enum_value (falling back
    to label) plus a synthetic <KEY>_THREAD member wherever thread_enum_value
    is set. Called once from app/models/text.py — see PlatformDefinition's
    has_text_prompt_rules docstring for why this isn't simply every
    "text"-pipeline platform."""
    from enum import Enum

    import_all()
    members: dict[str, str] = {}
    for p in list_platforms(pipeline="text"):
        if not p.has_text_prompt_rules:
            continue
        members[p.key.upper()] = p.text_enum_value or p.label
        if "thread" in p.shapes and p.thread_enum_value:
            members[f"{p.key.upper()}_THREAD"] = p.thread_enum_value
    return Enum(enum_name, members, type=str)


_IMPORTED = False


def import_all() -> None:
    """Import every module under app.platforms (and app.platforms.planned) so
    their @register_platform / register_platform() calls run. Idempotent —
    safe to call more than once (e.g. once from app startup, once from tests)."""
    global _IMPORTED
    if _IMPORTED:
        return
    import app.platforms as root_pkg

    for module_info in pkgutil.walk_packages(root_pkg.__path__, prefix=f"{root_pkg.__name__}."):
        if module_info.name.endswith(".base"):
            continue
        importlib.import_module(module_info.name)
    _IMPORTED = True
