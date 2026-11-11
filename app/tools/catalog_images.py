"""Tools for pictures beyond the hand edits in pictures.py: making new ones, templates, slides, sets, looks for a whole set, notes,
sharing and sign-off, plus the media library (uploaded files). Uploading a file needs the file itself, so uploads stay in the app (see
exemptions.py)."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

I = "/api/v1/image-assets"
_ID = f"{I}/{{image_asset_id}}"
_SLIDE = f"{_ID}/slides/{{slide_number}}"
M = "/api/v1/media"


def rows() -> list[T]:
    return [
        # reading
        T("image.list", "List the workspace's pictures, newest first.", Risk.READ, (f"GET {I}/",), examples=("Show my pictures",)),
        T("image.get", "Show one picture project with its slides, layers, looks and versions.", Risk.READ, (f"GET {_ID}",)),
        T("image.layouts", "List the picture shapes with their real pixel sizes.", Risk.READ, (f"GET {I}/layouts",), examples=("What sizes can a picture be?",)),
        T("image.icons", "List the icons that can be drawn on a picture.", Risk.READ, (f"GET {I}/icons",)),
        T("image.templates", "List the designs this workspace saved to start new pictures from.", Risk.READ, (f"GET {I}/templates",)),
        T("image.versions", "List a picture's saved versions.", Risk.READ, (f"GET {_ID}/versions",)),
        T("image.comments", "List the notes on a picture.", Risk.READ, (f"GET {_ID}/comments",)),
        T("image.share_links", "List a picture's share links that still work.", Risk.READ, (f"GET {_ID}/share-links",)),
        T("image.shapes", "Show a slide's main subject and how it would be cropped for each platform shape.", Risk.READ, (f"GET {_SLIDE}/shapes",)),
        T("image.contrast_check", "Check that a picture's text is easy to read on its background.", Risk.READ, (f"GET {_ID}/contrast-check",),
          examples=("Is the text on this picture readable?",)),
        # making
        T("image.generate", "Make a new picture from a description, with a headline in the brand's colours.", Risk.WRITE,
          (f"POST {I}/generate", f"POST {I}/generate/background"), cost_key="picture", limits=("pictures_day",),
          undo="Delete the picture with image.delete.", examples=("Make a picture for this post",)),
        T("image.add_slide", "Add one new slide to a carousel.", Risk.WRITE, (f"POST {_ID}/slides",), cost_key="picture", limits=("pictures_day",),
          undo="Remove the slide with image.delete_slide."),
        T("image.export", "Export a picture as PNG or WebP.", Risk.WRITE, (f"POST {_ID}/export",), note="Free."),
        T("image.apply_looks_to_set", "Draw the chosen looks on every picture of a set at once.", Risk.WRITE, (f"POST {_ID}/looks",),
          note="Free, except remove background costs 1 credit for each picture.", limits=("pictures_day",),
          undo="Switch the looks off again with the same tool."),
        T("image.send_to_draft", "Make a draft post from a picture project, with the pictures attached.", Risk.WRITE, (f"POST {_ID}/send-to-draft",),
          note="Free.", undo="Delete the draft with library.delete_post."),
        T("image.save_template", "Keep a picture's design as a template for new pictures.", Risk.WRITE, (f"POST {I}/templates",), note="Free.",
          undo="Remove it with image.delete_template."),
        # changing
        T("image.rename", "Rename a picture project.", Risk.WRITE, (f"PATCH {_ID}/rename",)),
        T("image.set_details", "Save a picture's link preview title and description, and its picture description for screen readers.",
          Risk.WRITE, (f"PATCH {_ID}/details",), undo="Save the earlier words again."),
        T("image.reorder_slides", "Put a carousel's slides in a new order.", Risk.WRITE, (f"PATCH {_ID}/slides/reorder",),
          undo="Put them back in the earlier order."),
        T("image.set_slide_role", "Mark what a slide does in the set: hook, stat or takeaway.", Risk.WRITE, (f"PATCH {_SLIDE}/role",)),
        # notes and sharing
        T("image.comment", "Add a note to a picture.", Risk.WRITE, (f"POST {_ID}/comments",)),
        T("image.edit_comment", "Change a note on a picture.", Risk.WRITE, (f"PATCH {_ID}/comments/{{comment_id}}",)),
        T("image.delete_comment", "Remove a note from a picture.", Risk.WRITE, (f"DELETE {_ID}/comments/{{comment_id}}",),
          undo="A removed note cannot be brought back."),
        T("image.share", "Make a share link so someone outside can see the picture and comment.", Risk.WRITE, (f"POST {_ID}/share-link",),
          undo="End the link with image.end_share_link."),
        T("image.end_share_link", "Stop a share link working at once.", Risk.WRITE, (f"DELETE {_ID}/share-link/{{token}}",),
          undo="Make a new link with image.share."),
        # removing
        T("image.delete", "Remove a picture project from History and the Library. Posts that use it keep their own copy.", Risk.DESTRUCTIVE,
          (f"DELETE {_ID}",), undo="It cannot be undone from here.", examples=("Delete that picture",)),
        T("image.delete_slide", "Remove one slide from a carousel and renumber the rest.", Risk.DESTRUCTIVE, (f"DELETE {_SLIDE}",),
          undo="Restore an earlier version with image.restore_version."),
        T("image.delete_template", "Remove a saved template. Pictures made from it are not touched.", Risk.DESTRUCTIVE,
          (f"DELETE {I}/templates/{{template_id}}",), undo="Save the design again with image.save_template."),
        # decisions, a person's call
        T("image.approve", "Approve a picture and pin its first slide as the master.", Risk.DECISION, (f"PATCH {_ID}/approve",)),
        T("image.reject", "Send a picture back.", Risk.DECISION, (f"PATCH {_ID}/reject",)),
        # media library
        T("media.get", "Show one uploaded file: its kind, size, link and what is known about it.", Risk.READ, (f"GET {M}/{{media_id}}",)),
        T("media.captions", "Get subtitles (SRT or WebVTT) built from a recording's words.", Risk.READ, (f"GET {M}/{{media_id}}/captions",),
          examples=("Give me subtitles for this video",)),
        T("media.analyze", "Write out what was said in an uploaded video or recording and find its chapters.", Risk.WRITE,
          (f"POST {M}/{{media_id}}/analyze",), cost_key="transcription", examples=("Transcribe this upload",)),
        T("media.transform", "Trim an uploaded video or crop it to a shape, as a new file.", Risk.WRITE, (f"POST {M}/{{media_id}}/transform",),
          note="Free.", undo="The original file is kept."),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
