"""Tools for video beyond the hand edits in videos.py: projects, rendering, versions, scripts and hooks, the member's voice, flows and
recipes, the video library, voice tracks and dubbing, transcripts, analysis, covers, stock footage, sound effects, reframing, consent and
privacy. Uploading a file, downloading a hand-off file or a subtitle file, and public sound files need the file itself, so they stay in the
app (see exemptions.py). The video actions (chain, director) are in actions.py."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

VP = "/api/v1/video-projects"
_P = f"{VP}/{{project_id}}"
VC = "/api/v1/video-create"
VF = "/api/v1/video-flows"
VM = "/api/v1/video-media"
VN = "/api/v1/video-narrate"
VT = "/api/v1/video-transcripts"
VA = "/api/v1/video-analysis"
VCV = "/api/v1/video-covers"
VS = "/api/v1/video-stock"
VX = "/api/v1/video-sfx"
VR = "/api/v1/video-reframe"
VG = "/api/v1/video-governance"
_FLOW_NOTE = "Priced by its steps (post, narration, picture, render); the estimate is shown first."


def rows() -> list[T]:
    return [
        # projects
        T("video.projects", "List the workspace's video projects, newest first.", Risk.READ, (f"GET {VP}",), examples=("Show my videos",)),
        T("video.get", "Show one video project: its clips, captions, music, canvas and sign-off.", Risk.READ, (f"GET {_P}",)),
        T("video.usage", "Show this month's render minutes and storage against the plan.", Risk.READ, (f"GET {VP}/usage",),
          examples=("How many render minutes do I have left?",)),
        T("video.job", "Show where a video job stands.", Risk.READ, (f"GET {VP}/jobs/{{job_id}}",), examples=("Is my render finished?",)),
        T("video.versions", "List a video project's saved versions.", Risk.READ, (f"GET {_P}/versions",)),
        T("video.history", "Show the whole story of a project in time order: edits, renders, sign-offs.", Risk.READ, (f"GET {_P}/history",)),
        T("video.renders", "List the renders made from a project and how each ended.", Risk.READ, (f"GET {_P}/renders",)),
        T("video.create_project", "Start a video project from library videos or pictures.", Risk.WRITE, (f"POST {VP}",), note="Free.",
          undo="Delete the project with video.delete_project.", examples=("Start a video from my last upload",)),
        T("video.render", "Render a project into a finished video, in the background.", Risk.WRITE, (f"POST {_P}/render",), cost_key="render",
          limits=("render_minutes_month",), examples=("Render this video",)),
        T("video.cancel_job", "Stop a video job that is still running.", Risk.WRITE, (f"POST {VP}/jobs/{{job_id}}/cancel",), note="Free.",
          examples=("Stop that render",)),
        T("video.reset", "Put a project back to its starting edit.", Risk.DESTRUCTIVE, (f"POST {_P}/reset",),
          undo="Bring an earlier version back with video.restore_version."),
        T("video.delete_project", "Remove a video project. The library videos it used stay.", Risk.DESTRUCTIVE, (f"DELETE {_P}",),
          undo="It cannot be undone from here.", examples=("Delete that video project",)),
        T("video.approve", "Sign off a project as it is now, or take the sign-off back.", Risk.DECISION, (f"PUT {_P}/approval",)),
        # scripts, hooks and the member's voice
        T("video.hooks", "Suggest openings for a video, from what is said in it.", Risk.WRITE, (f"POST {VC}/hooks",), note="Free."),
        T("video.script", "Write a script for a video from a topic.", Risk.WRITE, (f"POST {VC}/script",), cost_key="video_script"),
        T("video.script_from_pictures", "Write a script that walks through a set of pictures.", Risk.WRITE, (f"POST {VC}/script-from-pictures",),
          cost_key="video_script"),
        T("video.broll", "Suggest cutaway footage for the lines of a video.", Risk.WRITE, (f"POST {VC}/broll",), note="Free."),
        T("video.polish_captions", "Tidy caption wording without changing what is said.", Risk.WRITE, (f"POST {VC}/captions/polish",), note="Free."),
        T("video.publish_text", "Write the post that goes with a video for one platform.", Risk.WRITE, (f"POST {VC}/publish-text",), cost_key="text_post"),
        T("video.edit_plan", "Plan the edit of a video: cuts, pace and where to add things.", Risk.WRITE, (f"POST {VC}/edit-plan",), cost_key="video_plan"),
        T("video.critique", "Get an honest critique of a video with what to fix first.", Risk.WRITE, (f"POST {VC}/critique",), cost_key="draft_feedback"),
        T("video.voice_check", "Check whether the lines of a video sound like the person.", Risk.WRITE, (f"POST {VC}/voice-check",), note="Free."),
        T("video.records", "List what the video helpers made, newest first.", Risk.READ, (f"GET {VC}/records",)),
        T("video.recording_voice", "Show whether Ekavo is learning the person's voice from their own recordings.", Risk.READ,
          (f"GET {VC}/recording-voice",)),
        T("video.set_recording_voice", "Switch learning from the person's own recordings on or off.", Risk.WRITE, (f"PUT {VC}/recording-voice",),
          note="Free.", undo="Switch it back with the same tool."),
        T("video.learn_recording_voice", "Learn from the person's own recordings that have not been read yet.", Risk.WRITE,
          (f"POST {VC}/recording-voice/learn",), cost_key="voice_analysis"),
        T("video.forget_recording_voice", "Forget everything learned from the person's recordings.", Risk.DESTRUCTIVE, (f"DELETE {VC}/recording-voice",),
          undo="Learn again with video.learn_recording_voice."),
        # flows and recipes
        T("flows.catalog", "List the ready-made video flows and what each needs.", Risk.READ, (f"GET {VF}/catalog",), examples=("What video flows are there?",)),
        T("flows.recipes", "List the saved recipes: a flow with its settings kept.", Risk.READ, (f"GET {VF}/recipes",)),
        T("flows.run", "Run a flow once.", Risk.WRITE, (f"POST {VF}/run",), note=_FLOW_NOTE, limits=("render_minutes_month",)),
        T("flows.batch", "Run a flow over many files.", Risk.WRITE, (f"POST {VF}/batch",), note=_FLOW_NOTE, limits=("render_minutes_month",)),
        T("flows.save_recipe", "Keep a flow and its settings as a recipe.", Risk.WRITE, (f"POST {VF}/recipes",), note="Free.",
          undo="Remove it with flows.delete_recipe."),
        T("flows.run_recipe", "Run a saved recipe once, or over many files.", Risk.WRITE, (f"POST {VF}/recipes/{{recipe_id}}/run",), note=_FLOW_NOTE,
          limits=("render_minutes_month",)),
        T("flows.automate_recipe", "Make a recipe run by itself, daily or weekly, or stop it.", Risk.WRITE, (f"PUT {VF}/recipes/{{recipe_id}}/automation",),
          note=_FLOW_NOTE, undo="Switch it off with the same tool."),
        T("flows.delete_recipe", "Remove a saved recipe.", Risk.DESTRUCTIVE, (f"DELETE {VF}/recipes/{{recipe_id}}",),
          undo="Save it again with flows.save_recipe."),
        # the video library
        T("videolib.limits", "Show what a video upload may be: size, length and kinds.", Risk.READ, (f"GET {VM}/limits",)),
        T("videolib.pictures", "List the workspace's pictures, for putting into a video.", Risk.READ, (f"GET {VM}/pictures",)),
        T("videolib.usage", "Show where a library video is used: its projects and posts.", Risk.READ, (f"GET {VM}/{{media_id}}/usage",)),
        T("videolib.import_link", "Bring in a video from a Dropbox or Google Drive link.", Risk.WRITE, (f"POST {VM}/link-import",), note="Free.",
          undo="Remove it with videolib.delete."),
        T("videolib.preview", "Make the preview material for a library video that has none yet.", Risk.WRITE, (f"POST {VM}/{{media_id}}/preview",),
          note="Free."),
        T("videolib.delete", "Delete a library video and everything made from it.", Risk.DESTRUCTIVE, (f"DELETE {VM}/{{media_id}}",),
          undo="It cannot be undone from here.", examples=("Delete that upload",)),
        # voices, dubbing and captions in other languages
        T("videovoice.tracks", "List a video's voice tracks: the original and every dub.", Risk.READ, (f"GET {VN}/tracks/{{media_id}}",)),
        T("videovoice.dubs", "List the dubs made in this workspace.", Risk.READ, (f"GET {VN}/dub",)),
        T("videovoice.dub", "Show one dub and how far along it is.", Risk.READ, (f"GET {VN}/dub/{{dub_id}}",)),
        T("videovoice.dub_check", "Check a dub by listening back: does it say what the script says?", Risk.READ, (f"GET {VN}/dub/{{dub_id}}/check",)),
        T("videovoice.make_dub", "Make a video speak another language with a new voice track.", Risk.WRITE, (f"POST {VN}/dub",),
          note="Translation 1 credit for each 1,000 words, plus narration 3 credits a minute.", limits=("render_minutes_month",),
          undo="The original voice track is kept."),
        T("videovoice.narrate", "Voice a script over a video, fitted to its length.", Risk.WRITE, (f"POST {VN}/narration",), cost_key="narration",
          note="Narration 3 credits a minute, and 1 credit when the script is fitted to the video.", undo="The original voice track is kept."),
        T("videovoice.localize_captions", "Translate the captions of many videos at once.", Risk.WRITE, (f"POST {VN}/captions/localize",),
          cost_key="translate"),
        T("videovoice.captions_ready", "Show how many videos already have captions in a language.", Risk.READ, (f"GET {VN}/captions-ready",)),
        T("videovoice.captions", "Show a video's captions in each language it has.", Risk.READ, (f"GET {VN}/captions/{{media_id}}",)),
        # transcripts
        T("transcripts.estimate", "Show what transcribing a video would take, before it starts.", Risk.READ, (f"GET {VT}/{{media_id}}/estimate",)),
        T("transcripts.get", "Show a video's transcript and how far along it is.", Risk.READ, (f"GET {VT}/{{media_id}}",)),
        T("transcripts.words", "Page through a transcript's words with their timing.", Risk.READ, (f"GET {VT}/{{media_id}}/words",)),
        T("transcripts.search", "Find where something was said in a video.", Risk.READ, (f"GET {VT}/{{media_id}}/search",),
          examples=("Where do I mention pricing in this video?",)),
        T("transcripts.captions", "Show a video's captions built from its transcript.", Risk.READ, (f"GET {VT}/{{media_id}}/captions",)),
        T("transcripts.create", "Write out what was said in a video, in the background.", Risk.WRITE, (f"POST {VT}/{{media_id}}",),
          note="Uses the workspace's speech allowance first; the estimate shows what it will take.", examples=("Transcribe this video",)),
        T("transcripts.retry", "Try again the parts of a transcript that failed.", Risk.WRITE, (f"POST {VT}/{{media_id}}/retry",),
          note="Only the failed parts are done again."),
        # analysis
        T("videoanalysis.run", "Read a video and find its best moments, chapters and where it can be tightened.", Risk.WRITE, (f"POST {VA}/{{media_id}}",),
          cost_key="video_analysis", note="1 credit for each 10 minutes of video read.", examples=("Find the best moments in this video",)),
        T("videoanalysis.get", "Show what was found in a video: moments, chapters and notes.", Risk.READ, (f"GET {VA}/{{media_id}}",)),
        T("videoanalysis.cuts", "Show where a talk can be tightened: pauses, fillers and repeats.", Risk.READ, (f"GET {VA}/{{media_id}}/cuts",)),
        T("videoanalysis.shots", "List the shots a video is made of.", Risk.READ, (f"GET {VA}/{{media_id}}/shots",)),
        T("videoanalysis.chapters", "Get chapters in the form a video site reads.", Risk.READ, (f"GET {VA}/{{media_id}}/export",)),
        T("videoanalysis.set_moment", "Keep or skip one found moment.", Risk.WRITE, (f"POST {VA}/{{media_id}}/moments/{{moment_id}}",), note="Free.",
          undo="Set it back with the same tool."),
        # covers
        T("covers.platforms", "List the platforms a cover can be made for and their sizes.", Risk.READ, (f"GET {VCV}/platforms",)),
        T("covers.get", "Show the covers and frames already made for a video.", Risk.READ, (f"GET {VCV}/{{media_id}}",)),
        T("covers.frames", "Pick good frames from a video as cover candidates.", Risk.WRITE, (f"POST {VCV}/{{media_id}}/frames",), note="Free."),
        T("covers.make", "Make up to three cover options for one platform, each from a different moment.", Risk.WRITE, (f"POST {VCV}/{{media_id}}/covers",),
          note="Made as editable pictures; the picture price applies.", limits=("pictures_day",), examples=("Make a YouTube cover for this video",)),
        T("covers.compose", "Make covers for something that is not a video, such as an audio episode.", Risk.WRITE, (f"POST {VCV}/compose",),
          note="Made as editable pictures; the picture price applies.", limits=("pictures_day",)),
        # stock footage
        T("stock.status", "Show whether stock footage search is available.", Risk.READ, (f"GET {VS}/status",)),
        T("stock.search", "Search free stock footage by words.", Risk.READ, (f"GET {VS}/search",), examples=("Find stock footage of a sunrise",)),
        T("stock.rail", "List the lines of a video worth footage, each with a search to start from.", Risk.READ, (f"GET {VS}/rail/{{media_id}}",)),
        T("stock.rail_options", "Rank stock options for the chosen lines of a video.", Risk.WRITE, (f"POST {VS}/rail/options",), note="Free."),
        T("stock.use", "Save a stock clip to the workspace with its licence and author, and place it as a cutaway.", Risk.WRITE, (f"POST {VS}/use",),
          note="Free.", undo="Remove it with videolib.delete."),
        T("stock.credits", "List the credit lines for every stock clip a video used, ready to paste into a description.", Risk.READ,
          (f"GET {VS}/credits/{{project_id}}",)),
        # sound effects
        T("sfx.list", "List the sound effects that can be added to a video.", Risk.READ, (f"GET {VX}",), examples=("What sound effects are there?",)),
        T("sfx.add", "Put a sound effect in the workspace's library so a clip can use it.", Risk.WRITE, (f"POST {VX}/{{effect_id}}/add",), note="Free."),
        # reframing
        T("reframe.list", "List the reframes made: a wide video cut to follow the speaker.", Risk.READ, (f"GET {VR}",)),
        T("reframe.make", "Cut a wide video to a vertical shape that follows the speaker.", Risk.WRITE, (f"POST {VR}",), note="Free to start.",
          examples=("Make this vertical and follow the speaker",)),
        # prompt to video
        T("videoprompt.draft", "Write a script in scenes for a described video, with stock clips suggested for each scene.", Risk.WRITE,
          (f"POST /api/v1/video-prompt/draft",), cost_key="video_script"),
        T("videoprompt.make", "Save the chosen clips, speak every scene and open the project.", Risk.WRITE, (f"POST /api/v1/video-prompt/make",),
          note="Plan 3 credits and narration 3 credits a minute.", limits=("render_minutes_month",)),
        # consent and privacy
        T("videogov.privacy", "Show whether this workspace keeps its audio away from outside speech services.", Risk.READ, (f"GET {VG}/privacy",)),
        T("videogov.consents", "List the recorded consents for people's voices and faces.", Risk.READ, (f"GET {VG}/consents",)),
        T("videogov.set_privacy", "Keep this workspace's audio away from outside services, or allow it again. Owner or admin only.", Risk.ADMIN,
          (f"PUT {VG}/privacy",), undo="Change it back with the same tool."),
        T("videogov.add_consent", "Record that a person agreed to their voice or face being used.", Risk.DECISION, (f"POST {VG}/consents",)),
        T("videogov.withdraw_consent", "Withdraw a consent. It stays on the record.", Risk.DECISION, (f"DELETE {VG}/consents/{{consent_id}}",)),
        # client review links
        T("videoreview.links", "List the review links made for a video project and where each stands.", Risk.READ,
          ("GET /api/v1/video-review/projects/{project_id}/links",), examples=("Has my client reviewed the video?",)),
        T("videoreview.comments", "List every client comment on a video project, in video order.", Risk.READ,
          ("GET /api/v1/video-review/projects/{project_id}/comments",)),
        T("videoreview.create_link", "Make a link a client can open without an account, to watch a finished video and comment.", Risk.WRITE,
          ("POST /api/v1/video-review/projects/{project_id}/links",), note="Free.", undo="End the link with videoreview.end_link.",
          examples=("Send my client a review link",)),
        T("videoreview.end_link", "Stop a review link working at once.", Risk.WRITE, ("DELETE /api/v1/video-review/links/{link_id}",),
          undo="Make a new link with videoreview.create_link."),
        T("videoreview.resolve_comment", "Mark a client comment as dealt with, or open it again.", Risk.WRITE,
          ("PUT /api/v1/video-review/comments/{comment_id}/resolved",), undo="Open it again with the same tool."),
        T("videoreview.reply", "Reply to a client comment.", Risk.WRITE, ("POST /api/v1/video-review/comments/{comment_id}/replies",)),
        # prompt lists
        T("prompts.tones", "List the tones a post can be written in.", Risk.READ, ("GET /api/v1/prompt-lists/tones",),
          examples=("What tones can I write in?",)),
        # lineage
        T("lineage.get", "Show where an item came from and what was made from it.", Risk.READ, ("GET /api/v1/lineage/{kind}/{item_id}",),
          examples=("Where did this video come from?",)),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
