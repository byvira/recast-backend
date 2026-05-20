"""Brand blacklist checker — flags forbidden words in content."""


async def check_blacklist(content: str, forbidden_words: list[str]) -> dict:
    """Scan *content* for occurrences of any word in *forbidden_words*.

    Performs case-insensitive matching and returns the positions and
    suggested replacements for each flagged term.

    Args:
        content: Text content to scan.
        forbidden_words: List of words or phrases that must not appear.

    Returns:
        Dict with compliant (bool), matches (list of found terms), and
        suggestions (dict mapping each match to a replacement).
    """
    # Placeholder: tokenise content, match against forbidden_words list
    return {"compliant": True, "matches": [], "suggestions": {}}
