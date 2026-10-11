"""The look a member can ask a picture to have. Each profile is a plain-words style line added to what the image model is told, so
the choice changes the picture itself and is not just a label."""
from __future__ import annotations

VISUAL_PROFILE_STYLE: dict[str, str] = {
    "midnight_swiss": "Style: dark and minimal in the Swiss editorial manner. Deep midnight tones, plenty of empty space, one strong subject, crisp geometric shapes.",
    "cyber_editorial": "Style: vibrant and high contrast. Bold gradients with neon accents on a dark ground, a confident editorial composition.",
    "warm_analog": "Style: warm analog film. Soft grain, natural light, muted warm tones, a calm, human feel.",
    "mono_technical": "Style: monochrome technical graphic. Black and white line work like a blueprint, precise and clean, with at most one accent colour.",
}

VISUAL_PROFILES = tuple(VISUAL_PROFILE_STYLE)


def style_line(profile: str | None) -> str:
    """The style words for `profile`, or nothing for one that is not known."""
    return VISUAL_PROFILE_STYLE.get((profile or "").strip(), "")
