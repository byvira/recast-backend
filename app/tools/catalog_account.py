"""Tools for the brand (voices), the writing assistant, the activity log, analytics, supervisor insights, and read-only views of billing,
workspaces and the person's own profile. Creating API keys, webhooks, plan changes, payments and member changes stay in the app (see
exemptions.py)."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

B = "/api/v1/brand"
_B = f"{B}/{{brand_id}}"
AS = "/api/v1/assistant"
AC = "/api/v1/activity"
AN = "/api/v1/analytics"
BL = "/api/v1/billing"
WS = "/api/v1/workspaces"
SU = "/api/v1/supervisor"
US = "/api/v1/users"


def rows() -> list[T]:
    return [
        # brand: reading
        T("brand.get", "Show one brand profile: identity, voice, calibration, visuals, watermark and safety profile.", Risk.READ, (f"GET {_B}",)),
        T("brand.identity_fields", "List the fields each kind of brand has on its profile.", Risk.READ, (f"GET {B}/identity-fields",)),
        T("brand.counts", "Show what is linked to a brand: posts, pictures, recordings, videos and campaigns.", Risk.READ, (f"GET {_B}/counts",),
          examples=("How much is linked to this brand?",)),
        T("brand.reference_style", "Show what the brand's reference pictures look like.", Risk.READ, (f"GET {_B}/reference-style",)),
        # brand: making and changing
        T("brand.create", "Start a brand profile in the workspace.", Risk.WRITE, (f"POST {B}/",), note="Free; limited by the plan's brand voices.",
          limits=("brand_voices",), undo="Delete it in the app."),
        T("brand.duplicate", "Make a copy of a brand profile, never as the default.", Risk.WRITE, (f"POST {_B}/duplicate",), note="Free; limited by the plan's brand voices.",
          limits=("brand_voices",), undo="Delete the copy in the app."),
        T("brand.save_step", "Save the step a brand profile has reached in setup.", Risk.WRITE, (f"PUT {_B}/step",)),
        T("brand.set_type", "Correct the kind of brand while the profile is still being set up.", Risk.WRITE, (f"PATCH {_B}/type",)),
        T("brand.save_section", "Save one section of the profile: identity, audience, products and so on.", Risk.WRITE, (f"PATCH {_B}/sections/{{section}}",),
          undo="Save the earlier words again."),
        T("brand.set_language", "Set the language the brand writes in.", Risk.WRITE, (f"PATCH {_B}/language",), undo="Set it back with the same tool."),
        T("brand.set_safety_profile", "Choose the safety profile for the brand (for example kids or health).", Risk.WRITE, (f"PATCH {_B}/safety-profile",),
          undo="Set it back with the same tool."),
        T("brand.set_voice", "Edit the brand's tone and vocabulary directly.", Risk.WRITE, (f"PATCH {_B}/voice",), undo="Save the earlier words again."),
        T("brand.suggest_voice_patterns", "Draft a starting set of openers, closers and signature phrases from the brand's samples.", Risk.WRITE,
          (f"POST {_B}/suggest-voice-patterns",), cost_key="voice_analysis"),
        T("brand.preview_rewrite", "Try the brand's voice on a sample text, without saving anything.", Risk.WRITE, (f"POST {_B}/preview-rewrite",),
          cost_key="text_refine", examples=("Rewrite this in my brand voice",)),
        T("brand.set_calibration", "Save the brand's calibration: how formal, how bold, how long.", Risk.WRITE, (f"PATCH {_B}/calibration",),
          undo="Save the earlier settings again."),
        T("brand.set_visual_identity", "Save the brand's colours, fonts and picture style.", Risk.WRITE, (f"PATCH {_B}/visual-identity",),
          undo="Save the earlier settings again."),
        T("brand.set_logo", "Set the brand's logo from a picture already uploaded.", Risk.WRITE, (f"PUT {_B}/logo",), undo="Set the earlier logo again."),
        T("brand.set_watermark", "Change the brand's text and picture watermarks and where they go.", Risk.WRITE, (f"PATCH {_B}/watermark",),
          undo="Save the earlier settings again."),
        T("brand.watermark_suggestions", "Suggest five short text watermarks from the brand's own name.", Risk.WRITE, (f"POST {_B}/watermark/suggestions",),
          note="Free."),
        T("brand.make_watermark_mark", "Make a picture mark for the watermark with AI: a symbol, never words.", Risk.WRITE, (f"POST {_B}/watermark/mark",),
          note="Priced as a picture plus remove background.", limits=("pictures_day",)),
        T("brand.regenerate_mascot", "Make the brand's mascot again.", Risk.WRITE, (f"POST {_B}/visual-identity/mascot/regenerate",), cost_key="mascot"),
        T("brand.add_training_sample", "Add a real piece of the brand's own writing as a training sample.", Risk.WRITE, (f"POST {_B}/training-samples",),
          note="Free; when there is room, the voice traits are read from it for 1 credit.", undo="Remove it with brand.remove_training_sample."),
        T("brand.remove_training_sample", "Remove a training sample.", Risk.DESTRUCTIVE, (f"DELETE {_B}/training-samples/{{sample_id}}",),
          undo="Add the writing again with brand.add_training_sample."),
        T("brand.set_default", "Make a brand the workspace's default. Exactly one is the default.", Risk.WRITE, (f"PATCH {_B}/set-default",),
          undo="Make another brand the default."),
        T("brand.set_active", "Switch a brand on or off.", Risk.WRITE, (f"PATCH {_B}/set-active",), undo="Switch it back with the same tool."),
        T("brand.complete", "Mark a brand profile as finished.", Risk.WRITE, (f"PUT {_B}/complete",)),
        # writing assistant
        T("assistant.persona", "Show the person's own voice persona: their style, learned from their writing.", Risk.READ, (f"GET {AS}/persona",)),
        T("assistant.signals", "List the assistant's notes for the person, such as voice drift.", Risk.READ, (f"GET {AS}/signals",)),
        T("assistant.voice_settings", "Show the person's voice settings.", Risk.READ, (f"GET {AS}/voice-settings",)),
        T("assistant.lexicon", "Show the person's own words: pronunciations, jargon and writing rules.", Risk.READ, (f"GET {AS}/lexicon",)),
        T("assistant.nudge", "Show a quick, no-AI read on how well a post matches the person's voice.", Risk.READ, (f"GET {AS}/nudge",),
          examples=("Does this post sound like me?",)),
        T("assistant.acknowledge_signal", "Mark a note from the assistant as seen.", Risk.WRITE, (f"POST {AS}/signals/{{signal_id}}/acknowledge",), note="Free."),
        T("assistant.assist", "Tell the person what to fix in a draft in progress.", Risk.WRITE, (f"POST {AS}/assist",), cost_key="draft_feedback",
          examples=("What should I fix in this draft?",)),
        T("assistant.set_voice_settings", "Change the person's voice settings.", Risk.WRITE, (f"PUT {AS}/voice-settings",), undo="Save the earlier settings again."),
        T("assistant.set_lexicon", "Replace the person's own words list.", Risk.WRITE, (f"PUT {AS}/lexicon",), undo="Save the earlier list again."),
        # activity
        T("activity.list", "List what happened in the workspace, newest first.", Risk.READ, (f"GET {AC}",), examples=("What happened today?",)),
        T("activity.control_tower", "Show live work: what is running now and what just finished.", Risk.READ, (f"GET {AC}/control-tower",),
          examples=("What is running right now?",)),
        T("activity.inbox", "List what needs the person's attention.", Risk.READ, (f"GET {AC}/inbox",), examples=("What needs me?",)),
        T("activity.unread_count", "Show how many unread items need attention.", Risk.READ, (f"GET {AC}/unread-count",)),
        T("activity.attention", "Show the one number behind every needs-you badge.", Risk.READ, (f"GET {AC}/attention",)),
        T("activity.allowance", "Show what the workspace has left today of what has a daily limit.", Risk.READ, (f"GET {AC}/allowance",)),
        T("activity.autonomy", "Show how far Ekavo's advice can be trusted per platform, read only.", Risk.READ, (f"GET {AC}/autonomy",)),
        T("activity.mark_inbox_read", "Mark chosen inbox items, or all of them, as read.", Risk.WRITE, (f"POST {AC}/inbox/read",), note="Free."),
        T("activity.mark_read", "Mark activity rows as read.", Risk.WRITE, (f"POST {AC}/read",), note="Free."),
        T("activity.hide", "Delete rows from the person's own log. The shared record stays.", Risk.DESTRUCTIVE, (f"POST {AC}/hide",),
          undo="It cannot be undone from here."),
        T("activity.decide", "Answer an item waiting for a person's decision: accept or decline.", Risk.DECISION, (f"POST {AC}/{{entry_id}}/decision",)),
        T("activity.set_autonomy_threshold", "Change how sure Ekavo must be before it acts on its own.", Risk.DECISION, (f"PUT {AC}/autonomy/threshold",)),
        # analytics
        T("analytics.accounts", "List the connected accounts with their results.", Risk.READ, (f"GET {AN}/accounts",)),
        T("analytics.posts", "List published posts with their results.", Risk.READ, (f"GET {AN}/posts",), examples=("How did my posts do?",)),
        T("analytics.summary", "Summarise results over a period.", Risk.READ, (f"GET {AN}/summary",), examples=("How was last month?",)),
        T("analytics.pipeline_summary", "Show what each kind of work has made: totals and the last 7 days.", Risk.READ, (f"GET {AN}/pipeline-summary",)),
        T("analytics.asset_results", "Show how the posts one picture or recording went out in did.", Risk.READ, (f"GET {AN}/assets/{{asset_type}}/{{asset_id}}",)),
        T("analytics.top_performers", "List the workspace's top 5 percent of published posts by engagement.", Risk.READ, (f"GET {AN}/top-performers",),
          examples=("Which posts did best?",)),
        T("analytics.calendar", "List a month's posts arranged by day.", Risk.READ, (f"GET {AN}/calendar",)),
        T("analytics.ask_history", "List the recent questions asked of the analytics.", Risk.READ, (f"GET {AN}/ask",)),
        T("analytics.ask", "Ask a question of the workspace's results in plain words.", Risk.WRITE, (f"POST {AN}/ask",), cost_key="analytics_answer",
          examples=("Which platform works best for me?",)),
        T("analytics.refresh", "Pull the latest results from the connected platforms.", Risk.WRITE, (f"GET {AN}/refresh", f"POST {AN}/refresh"),
          note="Free; limited to a few times a minute."),
        T("analytics.record_results", "Save results typed in by hand for a published post.", Risk.WRITE, (f"PUT {AN}/posts/{{piece_id}}/self-reported",),
          note="Free.", undo="Save the earlier numbers again."),
        # billing, read only
        T("billing.plans", "Show the plans with prices in both currencies.", Risk.READ, (f"GET {BL}/plans",), examples=("What plans are there?",)),
        T("billing.subscription", "Show the plan in force and any change waiting for the renewal.", Risk.READ, (f"GET {BL}/subscription",)),
        T("billing.invoices", "List the receipts.", Risk.READ, (f"GET {BL}/invoices",)),
        T("billing.invoice", "Show one receipt, with how it was paid.", Risk.READ, (f"GET {BL}/invoices/{{invoice_id}}",)),
        T("billing.coverage", "Show which plan could cover a new workspace.", Risk.READ, (f"GET {BL}/coverage",)),
        T("billing.topups", "List the credit packs on sale and the bought credits not yet used.", Risk.READ, (f"GET {BL}/topups",)),
        T("billing.topup_quote", "Show what a number of credits would cost.", Risk.READ, (f"GET {BL}/topup/quote",), examples=("What would 500 credits cost?",)),
        # workspaces and people, read only
        T("workspaces.list", "List the workspaces the person belongs to.", Risk.READ, (f"GET {WS}/",)),
        T("workspaces.get", "Show one workspace.", Risk.READ, (f"GET {WS}/{{workspace_id}}",)),
        T("workspaces.entitlements", "Show the workspace's plan, every limit and how much is used.", Risk.READ, (f"GET {WS}/{{workspace_id}}/entitlements",),
          examples=("How much of my plan have I used?",)),
        T("workspaces.members", "List the members of a workspace and their roles.", Risk.READ, (f"GET {WS}/{{workspace_id}}/members",)),
        T("users.profile", "Show the person's own profile.", Risk.READ, (f"GET {US}/me",)),
        T("users.workspaces", "List every workspace the person is an active member of.", Risk.READ, (f"GET {US}/me/workspaces",)),
        T("users.public_profile", "Show the public profile of any user by username.", Risk.READ, (f"GET {US}/{{username}}",)),
        # supervisor
        T("supervisor.dashboard", "Show the supervisor's overview of the workspace.", Risk.READ, (f"GET {SU}/dashboard",)),
        T("supervisor.insights", "List the supervisor's insights.", Risk.READ, (f"GET {SU}/insights",)),
        T("supervisor.flags", "List what the supervisor flagged.", Risk.READ, (f"GET {SU}/flags",)),
        T("supervisor.notifications", "List the supervisor's notifications.", Risk.READ, (f"GET {SU}/notifications",)),
        T("supervisor.set_insight_status", "Mark an insight as seen, acted on or dismissed.", Risk.WRITE, (f"POST {SU}/insights/{{insight_id}}/status",),
          note="Free."),
        T("supervisor.set_flag_status", "Mark a flag as seen, handled or dismissed.", Risk.WRITE, (f"POST {SU}/flags/{{flag_id}}/status",), note="Free."),
        T("supervisor.read_notification", "Mark a supervisor notification as read.", Risk.WRITE, (f"POST {SU}/notifications/{{notif_id}}/read",), note="Free."),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
