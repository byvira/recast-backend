"""Tools for writing: refining, scoring and reviewing a post, fresh angles, saving text written elsewhere, repurpose history, the safety
check and search. Writing posts themselves (generate, regenerate, repurpose, batch) are the registered job actions (see actions.py)."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

TXT = "/api/v1/text"
RP = "/api/v1/repurposes"
CG = "/api/v1/content-guard"
SR = "/api/v1/search"
_INCLUDED = "Free: included with the post."


def rows() -> list[T]:
    return [
        # refining and checking a post
        T("text.refine", "Make a quick change to a post, such as shorter, friendlier or punchier.", Risk.WRITE, (f"POST {TXT}/refine",),
          cost_key="text_refine", undo="The earlier text stays in the post's versions.", examples=("Make it shorter", "Make it a bit friendlier")),
        T("text.refine_chat", "Talk a post through, turn by turn, asking for changes in your own words.", Risk.WRITE, (f"POST {TXT}/refine-chat",),
          cost_key="text_refine", undo="The earlier text stays in the post's versions.", examples=("Open with a question instead",)),
        T("text.reader_review", "Get a reader's honest reaction to a draft: what lands, what confuses.", Risk.WRITE, (f"POST {TXT}/reader-review",),
          cost_key="reader_review", examples=("How would a reader take this post?",)),
        T("text.score_hook", "Score a post's opening line and suggest stronger openings.", Risk.WRITE, (f"POST {TXT}/score-hook",),
          note=_INCLUDED, examples=("Is my first line strong?",)),
        T("text.score_readability", "Score how easy a post is to read.", Risk.WRITE, (f"POST {TXT}/score-readability",), note=_INCLUDED),
        T("text.angles", "Suggest three fresh angles for a post's wording.", Risk.WRITE, (f"POST {TXT}/angles",), note=_INCLUDED,
          examples=("Give me three other ways to say this",)),
        T("text.angle_ideas", "Plan three fresh angles for a picture, a recording or a video.", Risk.WRITE, (f"POST {TXT}/angles/media/ideas",),
          note="Free: planning the angles costs nothing; making them is priced.", examples=("Plan three new pictures for this post",)),
        T("text.angle_make", "Make the picked angles as new pictures, recordings or videos, in the background.", Risk.WRITE,
          (f"POST {TXT}/angles/media/make",), note="Priced by what is made: a picture 2 credits, narration 3 a minute, video 5 a minute.",
          limits=("pictures_day", "render_minutes_month"), examples=("Make the second angle",)),
        T("text.preview_link", "Read a link and show a short preview: its title, a snippet and its length.", Risk.WRITE, (f"POST {TXT}/preview-url",),
          note="Free.", examples=("What does this link say?",)),
        T("text.suggest_repurpose", "Suggest which platforms a piece of content would suit, before repurposing it.", Risk.WRITE,
          (f"POST {TXT}/repurpose/suggest",), note=_INCLUDED, examples=("Where should I repurpose this?",)),
        T("text.chips", "List the quick-change buttons available for a platform.", Risk.READ, (f"GET {TXT}/chips",),
          examples=("What quick changes can I make to a LinkedIn post?",)),
        # text written elsewhere
        T("library.create_draft", "Save text that was written elsewhere (for example by an assistant) as a real draft, with Ekavo's brand and safety checks.",
          Risk.WRITE, (f"POST {TXT}/pieces/manual",), note="Free: no AI call.", undo="Delete the draft with library.delete_post.",
          examples=("Save this as a LinkedIn draft",)),
        # repurpose history
        T("repurposes.list", "List what was made from a source, newest first.", Risk.READ, (f"GET {RP}",),
          examples=("What did I make from this blog post?",)),
        T("repurposes.save", "Note results made from one source. Saving the same result twice keeps one row.", Risk.WRITE, (f"POST {RP}",)),
        T("repurposes.remove", "Take a result off a source's list. The result itself is left as it is.", Risk.DESTRUCTIVE, (f"DELETE {RP}/{{record_id}}",),
          undo="The result is kept; note it again with repurposes.save."),
        # safety check
        T("safety.check", "Check a text against the safety rules, with dashes and filler removed and a safe rewrite when it can be made safe.",
          Risk.WRITE, (f"POST {CG}/check",), note="Free: safety checks never cost credits.", examples=("Is this post safe to publish?",)),
        T("safety.review_status", "Show the latest review request for a post the safety check held back, and its answer.", Risk.READ,
          (f"GET {CG}/review-requests/{{piece_id}}",), examples=("Did anyone look at my held post?",)),
        T("safety.request_review", "Ask a person to look at a post the safety check held back.", Risk.WRITE, (f"POST {CG}/review-requests",),
          note="Free.", examples=("Ask for a review of this post",)),
        # search
        T("search.find", "Search the workspace's posts, pictures, recordings and campaigns by words.", Risk.READ, (f"GET {SR}/",),
          examples=("Find my posts about pricing",)),
        T("search.ask", "Search with AI, which reads the workspace's own content and picks the best matches.", Risk.WRITE, (f"POST {SR}/agentic",),
          note="Free: search is included, with a daily limit.", examples=("Which post talked about our refund policy?",)),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
