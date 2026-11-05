"""The starting values of every quality check that Ops can change.

These are the values that run when nothing has been saved in Ops, Quality, and the values "Reset" goes back to. Platforms are
named the way the text platform enum writes them (LinkedIn, Twitter/X and so on).
"""
from __future__ import annotations

PLATFORMS: tuple[str, ...] = ("LinkedIn", "Twitter/X", "Twitter/X Thread", "Instagram", "Facebook", "Blog", "Newsletter", "YouTube")

# The shortest acceptable post, in words. 0 means no minimum. Below it the post is held and rewritten.
MIN_WORDS: dict[str, int] = {
    "Blog": 600, "Newsletter": 250, "LinkedIn": 150, "Twitter/X": 0, "Twitter/X Thread": 200,
    "Instagram": 100, "Facebook": 150, "YouTube": 100,
}

# The reading-ease score a post should reach (0 to 100, higher is easier). Below it the post only gets a note.
READABILITY: dict[str, int] = {
    "Blog": 45, "Newsletter": 50, "LinkedIn": 55, "Twitter/X": 65, "Twitter/X Thread": 65,
    "Instagram": 65, "Facebook": 55, "YouTube": 50,
}
READABILITY_FALLBACK = 45
MIN_WORDS_FALLBACK = 15

CTA_MARKERS: list[str] = [
    "?", "comment", "share", "follow", "subscribe", "link in bio",
    "check out", "read more", "click", "dm me", "let me know",
    "what do you think", "tag someone", "save this",
    "try", "open", "start", "use", "this sunday",
]
WEASEL_WORDS: list[str] = ["very", "really", "quite", "rather", "somewhat", "fairly", "basically", "literally"]
PASSIVE_PATTERNS: list[str] = [r"\bwas \w+ed\b", r"\bwere \w+ed\b", r"\bis being\b", r"\bbeen \w+ed\b"]

# Auto-publish trust. The four weights are percentages and add up to 100.
TRUST: dict[str, float] = {
    "min_decided": 10,
    "recommend_min_decisions": 20,
    "recommend_min_agreement": 90,
    "start_bar": 80,
    "weight_untouched": 35,
    "weight_kept": 25,
    "weight_quality": 20,
    "weight_delivered": 20,
}

# What counts as a pattern worth telling a member about.
PATTERNS: dict[str, float] = {
    "min_per_side": 3,
    "min_ratio": 1.5,
    "min_samples": 10,
    "max_samples": 300,
}
