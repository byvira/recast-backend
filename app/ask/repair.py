"""Quiet fixes for the small slips a planning model makes when it fills in a tool's inputs.

The model is told what each input is called. It still sometimes writes one platform where a list is needed, writes `facebook` where the tool
says `Facebook`, or writes `default` for a brand it cannot know the id of. Each of these has one clear meaning, so they are put right here
before the step is checked, and the person never sees a "not valid" answer for something that was plainly understood. A value that cannot be
read with confidence is left exactly as it is, so the check still refuses it."""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional, get_args, get_origin

from pydantic import BaseModel

PLACEHOLDER_BRANDS = {"", "default", "main", "primary", "my brand", "none", "null"}


def _enum_of(annotation: Any) -> Optional[type[Enum]]:
    """The enum an input takes, looking inside lists and optional values."""
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation
    for arg in get_args(annotation):
        found = _enum_of(arg)
        if found:
            return found
    return None


def _wants_list(annotation: Any) -> bool:
    if get_origin(annotation) in (list, set, tuple, frozenset):
        return True
    return any(_wants_list(a) for a in get_args(annotation))


def _plain(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _match(enum: type[Enum], value: Any) -> Any:
    """The enum's own value that this one means (same letters ignoring case and marks, or the one it begins), else the value unchanged."""
    if isinstance(value, Enum) or not isinstance(value, str):
        return value
    key = _plain(value)
    if not key:
        return value
    members = list(enum)
    for m in members:
        if _plain(m.value) == key or _plain(m.name) == key:
            return m.value
    starts = sorted((m for m in members if _plain(m.value).startswith(key)), key=lambda m: len(str(m.value)))
    return starts[0].value if starts else value


def repair_arguments(model: type[BaseModel], arguments: dict[str, Any]) -> dict[str, Any]:
    """The arguments with a single value turned into a list where a list is needed, and enum words put into the tool's own spelling."""
    args = dict(arguments)
    for name, field in model.model_fields.items():
        if name not in args or "${" in str(args[name]):
            continue
        value = args[name]
        if _wants_list(field.annotation) and not isinstance(value, (list, tuple, set)):
            if isinstance(value, str):
                value = [p.strip() for p in re.split(r"[,;]|\band\b", value) if p.strip()]
            elif value is not None:
                value = [value]
        enum = _enum_of(field.annotation)
        if enum is not None:
            value = [_match(enum, v) for v in value] if isinstance(value, list) else _match(enum, value)
        args[name] = value
    return args


async def default_brand_id(workspace_id: str) -> Optional[str]:
    from app.db.mongo import brand_profiles

    # only a finished brand can be used to make something; one still being set up is not offered
    row = await brand_profiles.find_one({"workspace_id": workspace_id, "is_complete": True}, {"id": 1}, sort=[("is_default", -1), ("created_at", 1)])
    return (row or {}).get("id")


def _picture_details(args: dict[str, Any]) -> None:
    """A picture asked for in a few words has a description and nothing else, so its title comes from the description, and it carries no headline text
    unless one was asked for."""
    prompt = str(args.get("prompt") or "").strip()
    if prompt and not str(args.get("title") or "").strip():
        args["title"] = (prompt[:60].rsplit(" ", 1)[0] if len(prompt) > 60 else prompt).strip().capitalize()
    if not str(args.get("headline") or "").strip():
        args["headline"] = ""
        args.setdefault("show_text", False)


def missing_brand(steps: list[Any], registry: Any) -> bool:
    """True when a step needs a brand and, after the repair, none was found for it."""
    for step in steps:
        spec = registry.get(step.tool)
        if spec is None or "brand_id" not in spec.input_model.model_fields:
            continue
        given = step.arguments.get("brand_id")
        if "${" not in str(given) and str(given or "").strip().lower() in PLACEHOLDER_BRANDS:
            return True
    return False


async def repair_steps(steps: list[Any], registry: Any, workspace_id: str) -> None:
    """Puts right every step's arguments in place. `default` (or nothing) for a brand becomes the workspace's main brand."""
    brand: Optional[str] = None
    looked = False
    for step in steps:
        spec = registry.get(step.tool)
        if spec is None:
            continue
        args = repair_arguments(spec.input_model, step.arguments)
        if "brand_id" in spec.input_model.model_fields:
            given = args.get("brand_id")
            if "${" not in str(given) and str(given or "").strip().lower() in PLACEHOLDER_BRANDS:
                if not looked:
                    brand, looked = await default_brand_id(workspace_id), True
                if brand:
                    args["brand_id"] = brand
        if step.tool == "image.generate":
            _picture_details(args)
        step.arguments = args
