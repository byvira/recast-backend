"""Tools for audio: recordings, narration, dialogue, transcripts, cleanup, chapters, soundbites, sharing, the podcast kit and feed, and
sign-offs. The registered job actions (transcribe, cleanup, assemble, video, regenerate, import, dialogue, director) are in actions.py."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

A = "/api/v1/audio-assets"
_ID = f"{A}/{{audio_asset_id}}"
_VERSION = "The earlier version stays in the version history."


def rows() -> list[T]:
    return [
        # reading
        T("audio.list", "List the workspace's recordings, newest first.", Risk.READ, (f"GET {A}/",), examples=("Show my recordings",)),
        T("audio.get", "Show one recording with its script, transcript, versions and sign-offs.", Risk.READ, (f"GET {_ID}",)),
        T("audio.versions", "List a recording's saved versions.", Risk.READ, (f"GET {_ID}/versions",)),
        T("audio.comments", "List the notes on a recording.", Risk.READ, (f"GET {_ID}/comments",)),
        T("audio.share_links", "List a recording's share links that still work.", Risk.READ, (f"GET {_ID}/share-links",)),
        T("audio.sound_checks", "Show a recording's loudness, peaks and long silences.", Risk.READ, (f"GET {_ID}/sound-checks",),
          examples=("Is this recording loud enough?",)),
        T("audio.render_jobs", "List the recent video renders for a recording and how each ended.", Risk.READ, (f"GET {_ID}/render-jobs",)),
        T("audio.soundbites", "List the soundbites found in a recording.", Risk.READ, (f"GET {_ID}/soundbites",), examples=("Show the soundbites",)),
        T("audio.video_clips", "List every video made from a recording in this workspace, newest first.", Risk.READ, (f"GET {A}/video-clips",)),
        T("audio.video_presets", "Show where a video is usually posted and what works there (shape and length).", Risk.READ, (f"GET {A}/video-presets",)),
        T("audio.run_status", "Show where a running audio job is. It is gone once it has finished.", Risk.READ, (f"GET {A}/runs/{{run_id}}",)),
        T("audio.kit", "Show a brand's podcast kit: intro, outro, music beds and sponsor script.", Risk.READ, (f"GET {A}/kit/{{brand_id}}",)),
        T("audio.feed_status", "Show whether the podcast feed is on and where it stands.", Risk.READ, (f"GET {A}/feed/status",),
          examples=("Is my podcast feed on?",)),
        T("audio.promotion_estimate", "Show what promoting a recording would make and cost, before anything is made.", Risk.READ,
          (f"GET {_ID}/promotion/estimate",), examples=("How much would promoting this episode cost?",)),
        # making
        T("audio.generate", "Voice a script as a new recording.", Risk.WRITE, (f"POST {A}/generate", f"POST {A}/generate/background"),
          cost_key="narration", undo="Delete the recording with audio.delete.", examples=("Narrate this script",)),
        T("audio.localize", "Translate a recording's script and voice it again in another language.", Risk.WRITE, (f"POST {_ID}/localize",),
          note="Translation 1 credit for each 1,000 words, plus narration 3 credits a minute.", undo=_VERSION),
        T("audio.fit_script", "Rewrite a script to run about a chosen length.", Risk.WRITE, (f"POST {A}/fit-script",), cost_key="audio_script"),
        T("audio.length_preview", "Show how long a script will run, before any narration is made.", Risk.WRITE, (f"POST {A}/length-preview",),
          note="Free."),
        T("audio.chapters", "Find where the topic changes and save chapters.", Risk.WRITE, (f"POST {_ID}/chapters",), cost_key="audio_script"),
        T("audio.derivatives", "Write show notes, captions or a short summary from a recording.", Risk.WRITE, (f"POST {_ID}/derivatives",),
          cost_key="audio_script", examples=("Write show notes for this episode",)),
        T("audio.suggest_clips", "Suggest the best moments to clip. Suggestions only, nothing is applied.", Risk.WRITE, (f"POST {_ID}/suggest-clips",),
          cost_key="audio_script"),
        T("audio.find_soundbites", "Find the quotable moments and cut each one out as a soundbite.", Risk.WRITE, (f"POST {_ID}/soundbites",),
          cost_key="audio_script", examples=("Find the soundbites in this episode",)),
        T("audio.refine_soundbites", "Change the soundbites with a plain instruction, such as shorter or funnier.", Risk.WRITE,
          (f"POST {_ID}/soundbites/refine",), cost_key="audio_script"),
        T("audio.promote", "Make draft posts and quote pictures from a recording's best moments. Everything is a draft; nothing is published.",
          Risk.WRITE, (f"POST {_ID}/promotion",), note="1 credit to find moments, 1 for each post, 2 for each picture.", limits=("pictures_day",),
          undo="Delete the drafts with library.delete_post.", examples=("Promote this episode on LinkedIn",)),
        T("audio.send_to_draft", "Make a draft post from one of a recording's videos, with its caption and the video attached.", Risk.WRITE,
          (f"POST {_ID}/send-to-draft",), note="Free.", undo="Delete the draft with library.delete_post."),
        T("audio.list_feed_episodes", "List the episodes in a podcast feed so one can be picked for import.", Risk.WRITE, (f"POST {A}/import/feed",),
          note="Free: nothing is saved."),
        # changing a recording
        T("audio.rename", "Rename a recording.", Risk.WRITE, (f"PATCH {_ID}/rename",)),
        T("audio.correct_word", "Correct one word of a transcript. Its timing is not changed.", Risk.WRITE, (f"PATCH {_ID}/transcript/word",),
          undo="Correct it again."),
        T("audio.export", "Export a recording in another format (for example MP3 or WAV) as a new file.", Risk.WRITE, (f"POST {_ID}/export",),
          note="Free."),
        T("audio.restore_version", "Bring back an earlier version of a recording as a new version.", Risk.WRITE, (f"POST {_ID}/restore/{{version_number}}",),
          undo="Restore the version that was replaced."),
        T("audio.update_video_clip", "Change the details of one video made from a recording.", Risk.WRITE, (f"PATCH {_ID}/video-clips/{{clip_id}}",)),
        # notes and sharing
        T("audio.comment", "Add a note to a recording.", Risk.WRITE, (f"POST {_ID}/comments",)),
        T("audio.edit_comment", "Change a note on a recording.", Risk.WRITE, (f"PATCH {_ID}/comments/{{comment_id}}",)),
        T("audio.delete_comment", "Remove a note from a recording.", Risk.WRITE, (f"DELETE {_ID}/comments/{{comment_id}}",),
          undo="A removed note cannot be brought back."),
        T("audio.share", "Make a share link so someone outside can listen and comment.", Risk.WRITE, (f"POST {_ID}/share-link",),
          undo="End the link with audio.end_share_link."),
        T("audio.update_share_link", "Turn guest comments on or off for one share link.", Risk.WRITE, (f"PATCH {_ID}/share-link/{{token}}",)),
        T("audio.end_share_link", "Stop a share link working at once.", Risk.WRITE, (f"DELETE {_ID}/share-link/{{token}}",),
          undo="Make a new link with audio.share."),
        # podcast kit and feed
        T("audio.save_kit", "Save a brand's podcast kit settings.", Risk.WRITE, (f"PUT {A}/kit/{{brand_id}}",), undo="Save the earlier settings again."),
        T("audio.add_bed", "Add a music bed to a brand's podcast kit.", Risk.WRITE, (f"POST {A}/kit/{{brand_id}}/beds",)),
        T("audio.remove_bed", "Remove a music bed from a brand's podcast kit.", Risk.WRITE, (f"DELETE {A}/kit/{{brand_id}}/beds/{{bed_id}}",),
          undo="Add it again with audio.add_bed."),
        T("audio.sponsor_voice", "Voice the saved sponsor script in the person's own voice.", Risk.WRITE,
          (f"POST {A}/kit/{{brand_id}}/sponsor/synthesize",), cost_key="narration"),
        T("audio.enable_feed", "Turn on the public podcast feed for a brand. Episodes then appear in podcast apps.", Risk.PUBLISH,
          (f"POST {A}/feed/enable",), undo="Turn the feed off with audio.update_feed.", examples=("Turn on my podcast feed",)),
        T("audio.update_feed", "Change the public podcast feed's settings or turn it off.", Risk.PUBLISH, (f"PATCH {A}/feed/{{brand_id}}",),
          undo="Change it back with the same tool."),
        # removing
        T("audio.delete", "Remove a recording, and the videos made from it, from History.", Risk.DESTRUCTIVE, (f"DELETE {_ID}",),
          undo="It cannot be undone from here.", examples=("Delete that recording",)),
        T("audio.delete_video_clip", "Remove one video made from a recording. The recording stays.", Risk.DESTRUCTIVE,
          (f"DELETE {_ID}/video-clips/{{clip_id}}",), undo="It cannot be undone from here; make the video again."),
        # decisions, a person's call
        T("audio.approve", "Approve a recording's master version.", Risk.DECISION, (f"PATCH {_ID}/approve",)),
        T("audio.reject", "Send a recording back.", Risk.DECISION, (f"PATCH {_ID}/reject",)),
        T("audio.sign_off", "Sign off a recording for one role (for example sound or script).", Risk.DECISION, (f"POST {_ID}/signoff",)),
        T("audio.remove_sign_off", "Take back a sign-off. The person who signed, or an owner or admin, can.", Risk.DECISION,
          (f"DELETE {_ID}/signoff/{{role}}",)),
        T("audio.approve_soundbite", "Approve a soundbite.", Risk.DECISION, (f"PATCH {A}/soundbites/{{soundbite_id}}/approve",)),
        T("audio.reject_soundbite", "Send a soundbite back.", Risk.DECISION, (f"PATCH {A}/soundbites/{{soundbite_id}}/reject",)),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
