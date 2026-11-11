"""Tools for the Library: posts, writing runs, documents, versions, attachments, scheduling, approval and deleting."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

C = "/api/v1/content"
_SUPPORT_ONLY = "Only support can restore a deleted post."
_VERSIONS = "The earlier text stays in the post's versions; bring it back with library.restore_version."


def rows() -> list[T]:
    return [
        # reading
        T("library.document", "Read a post's full document, such as a long blog or newsletter.", Risk.READ, (f"GET {C}/pieces/{{piece_id}}/doc",),
          examples=("Show me the whole blog post",)),
        T("library.versions", "List a post's saved versions.", Risk.READ, (f"GET {C}/pieces/{{piece_id}}/versions",),
          examples=("What earlier versions of this post exist?",)),
        T("library.attachments", "List the pictures, recordings and videos attached to a post.", Risk.READ,
          (f"GET {C}/pieces/{{piece_id}}/attachments",), examples=("What is attached to this post?",)),
        T("library.runs", "List writing runs (one request that made posts), newest first.", Risk.READ, (f"GET {C}/sessions",),
          examples=("What did I write last week?",)),
        T("library.run", "Show one writing run with the posts it made.", Risk.READ, (f"GET {C}/sessions/{{session_id}}",),
          examples=("Show me what that run made",)),
        T("library.export_run", "Export one writing run's posts.", Risk.READ, (f"GET {C}/sessions/{{session_id}}/export",),
          examples=("Export that run as a spreadsheet",)),
        T("library.export_all", "Export the Library's posts.", Risk.READ, (f"GET {C}/export",), examples=("Export all my posts",)),
        # changing posts
        T("library.edit_post", "Change a post's text or details.", Risk.WRITE, (f"PATCH {C}/pieces/{{piece_id}}",), undo=_VERSIONS,
          examples=("Change the first line of this post",)),
        T("library.save_document", "Save the full document of a long post.", Risk.WRITE, (f"PUT {C}/pieces/{{piece_id}}/doc",), undo=_VERSIONS),
        T("library.restore_version", "Bring back an earlier version of a post as a new version.", Risk.WRITE,
          (f"POST {C}/pieces/{{piece_id}}/restore/{{version_number}}",), undo="Restore the version that was replaced.",
          examples=("Go back to the version from yesterday",)),
        T("library.archive_post", "Archive a post, or bring it back from the archive.", Risk.WRITE, (f"PATCH {C}/pieces/{{piece_id}}/archive",),
          undo="Run it again to bring the post back."),
        T("library.cancel_schedule", "Cancel a post's scheduled time, so it goes back to a draft.", Risk.WRITE,
          (f"POST {C}/pieces/{{piece_id}}/cancel-schedule",), undo="Schedule it again with library.schedule_post.",
          examples=("Don't post that tomorrow after all",)),
        T("library.mark_posted", "Record that a post was published by hand, with its link.", Risk.WRITE, (f"POST {C}/pieces/{{piece_id}}/mark-posted",),
          examples=("I posted this myself, here is the link",)),
        T("library.rename_run", "Rename a writing run.", Risk.WRITE, (f"PATCH {C}/sessions/{{session_id}}",)),
        T("library.write_first_comment", "Write a first comment for a post, from its caption.", Risk.WRITE, (f"POST {C}/first-comment",),
          note="Free: it is part of getting a post ready, with a daily limit for each member.", examples=("Write a first comment for this post",)),
        T("library.post_feedback", "Tell Ekavo what you thought of a post (it helps your voice assistant learn).", Risk.WRITE,
          (f"POST {C}/pieces/{{piece_id}}/feedback",)),
        T("library.check_link", "Check that the link in a post works.", Risk.WRITE, (f"POST {C}/pieces/{{piece_id}}/check-link",),
          examples=("Does the link in this post work?",)),
        T("library.regenerate_picture", "Make a post's picture again.", Risk.WRITE, (f"POST {C}/pieces/{{piece_id}}/regenerate-picture",),
          cost_key="picture", limits=("pictures_day",), undo="The earlier picture stays in the picture's version history.",
          examples=("Make a new picture for this post",)),
        # attachments
        T("library.attach", "Attach a picture, recording or video to a post.", Risk.WRITE, (f"POST {C}/pieces/{{piece_id}}/attachments",),
          undo="Take it off again with library.detach.", examples=("Attach this picture to the post",)),
        T("library.update_attachment", "Change an attachment's details, such as its description for screen readers.", Risk.WRITE,
          (f"PATCH {C}/pieces/{{piece_id}}/attachments/{{attachment_id}}",)),
        T("library.detach", "Take an attachment off a post. The picture, recording or video itself is kept.", Risk.WRITE,
          (f"DELETE {C}/pieces/{{piece_id}}/attachments/{{attachment_id}}",), undo="Attach it again with library.attach."),
        T("library.reorder_attachments", "Put a post's attachments in a new order. The first becomes the main one.", Risk.WRITE,
          (f"PUT {C}/pieces/{{piece_id}}/attachments/order",)),
        T("library.refresh_attachments", "Bring a post's attachments up to date with their latest versions.", Risk.WRITE,
          (f"POST {C}/pieces/{{piece_id}}/attachments/refresh",)),
        T("library.set_post_media", "Set the picture or media a post is published with.", Risk.WRITE, (f"PATCH {C}/pieces/{{piece_id}}/media",)),
        # scheduling
        T("library.schedule_post", "Schedule an approved post for a time, or send it for approval first.", Risk.PUBLISH,
          (f"PATCH {C}/pieces/{{piece_id}}/schedule",), human_fields=("confirm_publish_anyway", "confirm_send"),
          undo="A post that has not gone out can be cancelled with library.cancel_schedule.",
          examples=("Schedule this for Tuesday at 9",)),
        # deleting
        T("library.delete_post", "Delete a post.", Risk.DESTRUCTIVE, (f"DELETE {C}/pieces/{{piece_id}}",), undo=_SUPPORT_ONLY,
          examples=("Delete that post",)),
        T("library.delete_run", "Delete a writing run and its posts that have not gone out. Posts already published are kept.", Risk.DESTRUCTIVE,
          (f"DELETE {C}/sessions/{{session_id}}",), undo="It cannot be undone from here; published posts are kept."),
        # decisions, a person's call
        T("library.approve_post", "Approve a draft so it can be scheduled or published.", Risk.DECISION, (f"PATCH {C}/pieces/{{piece_id}}/approve",),
          examples=("Approve this post",)),
        T("library.reject_post", "Send a draft back, with a reason.", Risk.DECISION, (f"PATCH {C}/pieces/{{piece_id}}/reject",),
          examples=("Send this back, it is off brand",)),
        T("library.approve_many", "Approve several drafts at once.", Risk.DECISION, (f"POST {C}/pieces/approve-many",)),
        T("library.approve_waiting", "Approve every draft that is waiting for review.", Risk.DECISION, (f"POST {C}/approve-waiting",)),
        T("library.approve_run", "Approve every draft of one writing run.", Risk.DECISION, (f"PATCH {C}/sessions/{{session_id}}/approve-all",)),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
