"""Tools for campaigns, templates (presets) and the notes people leave on editors. Making the next batch of a campaign and making a post's
picture again are the registered job actions (see actions.py). Reading a dropped document, uploading a thumbnail and downloading the
campaign ZIP need the file itself, so they stay in the app (see exemptions.py)."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

C = "/api/v1/campaigns"
_ID = f"{C}/{{campaign_id}}"
P = "/api/v1/presets"
EC = "/api/v1/editor-comments"


def rows() -> list[T]:
    return [
        # campaigns
        T("campaigns.get", "Show one campaign: its plan, progress and posts.", Risk.READ, (f"GET {_ID}",), examples=("How is my launch campaign doing?",)),
        T("campaigns.media", "List the pictures and recordings made for a campaign's posts.", Risk.READ, (f"GET {_ID}/media",)),
        T("campaigns.next_batch_preview", "Show what making the next batch would do and cost, before anything is made.", Risk.READ,
          (f"GET {_ID}/next-batch",), examples=("What would the next batch cost?",)),
        T("campaigns.video_sources", "List the videos in the library a campaign can start from.", Risk.READ, (f"GET {C}/video-sources",)),
        T("campaigns.recording_sources", "List the recordings in the workspace a campaign can start from.", Risk.READ, (f"GET {C}/recording-sources",)),
        T("campaigns.create", "Start a campaign: a topic, the platforms and how many days or posts.", Risk.WRITE, (f"POST {C}/",),
          note="Free to create; making its batches is priced.", limits=("campaigns_active",), undo="Delete it with campaigns.delete.",
          examples=("Start a two week launch campaign",)),
        T("campaigns.update", "Change a campaign's name, plan or settings.", Risk.WRITE, (f"PATCH {_ID}",), undo="Change it back with the same tool."),
        T("campaigns.suggest_topics", "Suggest topics for a campaign from the brand and what it has already posted.", Risk.WRITE,
          (f"POST {C}/suggest-topics",), note="Free.", examples=("Suggest campaign topics",)),
        T("campaigns.delete", "Remove a campaign. A batch still being made for it is stopped.", Risk.DESTRUCTIVE, (f"DELETE {_ID}",),
          undo="Only support can restore a deleted campaign.", examples=("Delete the old launch campaign",)),
        # templates
        T("presets.list", "List the saved templates for posts.", Risk.READ, (f"GET {P}/",), examples=("Show my templates",)),
        T("presets.get", "Show one saved template.", Risk.READ, (f"GET {P}/{{preset_id}}",)),
        T("presets.suggest_structure", "Suggest the sections of a template from its name and kind.", Risk.WRITE, (f"POST {P}/suggest-structure",),
          note="Free."),
        T("presets.create", "Save a template for posts.", Risk.WRITE, (f"POST {P}/",), undo="Delete it with presets.delete."),
        T("presets.update", "Change a saved template.", Risk.WRITE, (f"PATCH {P}/{{preset_id}}",), undo="Change it back with the same tool."),
        T("presets.delete", "Remove a saved template. Posts made from it are not touched.", Risk.DESTRUCTIVE, (f"DELETE {P}/{{preset_id}}",),
          undo="Save the template again with presets.create."),
        # notes on editors
        T("notes.list", "List the notes left on an editor, with their replies.", Risk.READ, (f"GET {EC}",), examples=("What notes are on this video?",)),
        T("notes.add", "Leave a note on a picture, recording or video.", Risk.WRITE, (f"POST {EC}",), undo="Remove it with notes.remove."),
        T("notes.reply", "Reply to a note.", Risk.WRITE, (f"POST {EC}/{{comment_id}}/replies",)),
        T("notes.resolve", "Mark a note as dealt with, or open it again.", Risk.WRITE, (f"PUT {EC}/{{comment_id}}/resolved",),
          undo="Open it again with the same tool."),
        T("notes.remove", "Remove a note.", Risk.WRITE, (f"DELETE {EC}/{{comment_id}}",), undo="A removed note cannot be brought back."),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
