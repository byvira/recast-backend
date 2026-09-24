"""Odette — the workspace supervisor's fixed persona.

Distinct from Remy (the personal assistant). Odette speaks admin-to-admin about
the whole workspace: crisp, senior chief-of-staff, signal-over-noise, always
quantifies, always ends on a recommended action. Never a generic system alert.
"""

from app.pipelines.text.generator import resolve_language_name
from app.prompts.registry import load_localized, load_prompt

ODETTE_NAME = "Odette"

ODETTE_TONE = (
    "crisp, senior chief-of-staff addressing a workspace admin as a peer; "
    "signal over noise; quantifies everything; every item ends with a concrete "
    "recommended action; never alarmist, never a generic system alert"
)


def build_odette_system(language: str = "en") -> str:
    """Odette's system prompt, with the language directive baked into the
    same template rather than concatenated on afterward.

    Unlike Remy's/Odette's templated flag copy (see odette_flag_summary below),
    Odette's insights/flag-synthesis text here is LLM-generated, not a fixed
    template — this is a prompt instruction, not a translation-cache lookup.

    `language` is a fully opaque string here — never validated or matched
    against LANGUAGE_NAMES or any other fixed set. Uses the same
    resolve_language_name() as every other language-directive site in the
    codebase (generator.py, repurpose.py, normalizer.py, analytics/nodes.py,
    personal/assist.py, personal/nodes.py).

    Renders app/prompts/supervisor/odette_system.jinja. No
    `if name == "English": ...` branch — every language, "en" included, gets
    the identical rendered shape.
    """
    name = resolve_language_name(language)
    return load_prompt("supervisor/odette_system", name=name)


# ─────────────────────────────────────────────────────────────────────────────
# odette_flag_summary — English source templates, translated into `language`
# on demand and cached in Mongo via app.shared.localized_strings. Replaces
# the earlier static en/ta/hi/ko dict-of-functions approach entirely, same as
# signals.py's remy_message(): `language` is a fully opaque string here, not
# validated or matched against any fixed set.
# ─────────────────────────────────────────────────────────────────────────────

# Source-of-truth English text lives in app/prompts/localized/odette_flags.yaml
# — get_localized_string() takes the template as a plain string argument, so
# it doesn't care whether that string came from a dict literal or a YAML
# file; no change needed there.
_ODETTE_FLAG_ENGLISH_TEMPLATES: dict[str, str] = load_localized("odette_flags")

_MEMBER_CHURN_DEFAULT = "Unusual membership change"
_SIGNAL_STORM_DEFAULT = "A burst of assistant signals"


async def odette_flag_summary(flag_type: str, detail: dict, language: str = "en") -> str:
    """Odette-voice one-liner for a rule flag, translated into `language` on
    demand and cached — see app.shared.localized_strings.get_localized_string().
    """
    from app.shared.localized_strings import get_localized_string

    d = detail or {}
    known = flag_type in _ODETTE_FLAG_ENGLISH_TEMPLATES
    template = _ODETTE_FLAG_ENGLISH_TEMPLATES.get(flag_type, _ODETTE_FLAG_ENGLISH_TEMPLATES["__fallback__"])
    key = f"odette.flag.{flag_type if known else '__fallback__'}"

    if flag_type == "tier_seat_exceeded":
        format_ctx = {
            "active_members": d.get("active_members"), "tier": d.get("tier"), "seats": d.get("seats"),
            "overage": (d.get("active_members", 0) or 0) - (d.get("seats", 0) or 0),
        }
    elif flag_type == "daily_publish_cap":
        format_ctx = {"count": d.get("count"), "cap": d.get("cap"), "tier": d.get("tier")}
    elif flag_type == "rbac_violation":
        format_ctx = {"actor_role": d.get("actor_role"), "event_type": d.get("event_type")}
    elif flag_type == "brand_voice_instability":
        format_ctx = {"count": d.get("count")}
    elif flag_type == "member_churn":
        format_ctx = {"summary": d.get("summary", _MEMBER_CHURN_DEFAULT)}
    elif flag_type == "assistant_signal_storm":
        format_ctx = {"summary": d.get("summary", _SIGNAL_STORM_DEFAULT)}
    elif flag_type == "platform_capability_drift":
        from app.shared.activity.projector import platform_name
        names = [platform_name(p.get("platform")) for p in (d.get("platforms") or []) if p.get("platform")]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}" if names else "A platform"
        format_ctx = {"platforms": joined, "verb": "is" if len(names) <= 1 else "are",
                      "pronoun": "it" if len(names) <= 1 else "them"}
    elif flag_type == "platform_delivery_failing":
        from app.shared.activity.projector import platform_name
        format_ctx = {"platform": platform_name(d.get("platform")), "failures": d.get("failures"),
                      "window_hours": d.get("window_hours")}
    elif flag_type == "connection_broken":
        format_ctx = {"platform": d.get("platform"), "account": d.get("account"), "failures": d.get("failures")}
    else:
        format_ctx = {"flag_type_label": flag_type.replace("_", " "), "detail": str(d)}

    return await get_localized_string(key, language, template, format_ctx)
