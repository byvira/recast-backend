"""GeneratedPiece strips em dashes unconditionally — see app/models/text.py.

Em dashes are a well-known LLM writing tell users flagged repeatedly as
"AI slop"; prompt instructions alone don't reliably stop models from using
them, so every GeneratedPiece construction path (generate, repurpose,
refine, rewrite-on-retry) is covered by a field validator instead of
hunting down each call site.
"""

from app.models.text import GeneratedPiece, Platform, strip_em_dashes


def test_strip_em_dashes_replaces_with_comma():
    assert strip_em_dashes("great — impactful") == "great, impactful"
    assert strip_em_dashes("great—impactful") == "great, impactful"


def test_strip_em_dashes_is_a_noop_without_one():
    assert strip_em_dashes("no dashes here.") == "no dashes here."


def test_generated_piece_content_never_contains_em_dash():
    piece = GeneratedPiece(
        platform=Platform.LINKEDIN,
        content="This changed everything — for real.",
        word_count=0,  # deliberately wrong — must be recomputed, not trusted
        char_count=0,
    )
    assert "—" not in piece.content
    assert piece.content == "This changed everything, for real."


def test_generated_piece_counts_sync_to_stripped_content():
    piece = GeneratedPiece(
        platform=Platform.LINKEDIN,
        content="a — b",
        word_count=999,
        char_count=999,
    )
    assert piece.word_count == len(piece.content.split())
    assert piece.char_count == len(piece.content)
