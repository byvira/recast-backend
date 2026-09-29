"""Help guides shown on the Support page and used to ground support answers.

One source of truth: the Support page reads these from ``GET /support/guides``
and AI drafts search them, so what members read and what staff drafts quote
can never disagree. Every statement here describes something Recast does
today. Where a feature is not built yet the guide says so plainly. Wording
follows the product copy rules: plain language, no em dashes, no jargon.
"""

from __future__ import annotations

import re

TOPICS: list[dict] = [
    {"id": "getting-started", "label": "Getting started"},
    {"id": "brand-voice", "label": "Brand voice"},
    {"id": "writing", "label": "Writing posts"},
    {"id": "audio-image", "label": "Audio and images"},
    {"id": "campaigns", "label": "Campaigns"},
    {"id": "publishing", "label": "Publishing"},
    {"id": "accounts", "label": "Connected accounts"},
    {"id": "team", "label": "Team and settings"},
    {"id": "help", "label": "Help and safety"},
]


def _g(id: str, topic: str, title: str, summary: str, body: str, keywords: str = "") -> dict:
    return {"id": id, "topic": topic, "title": title, "summary": summary, "body": body, "keywords": keywords}


GUIDES: list[dict] = [
    # ── Getting started ──────────────────────────────────────────────────────
    _g("what-recast-does", "getting-started", "What Recast does",
       "Describe an idea once and get posts ready for each platform, in your own voice.",
       "You give Recast an idea, a link or some raw text. It writes a post for each platform you choose, using your brand voice, and puts every piece in a review queue. Nothing is published until someone on your team approves it.",
       "overview intro how it works"),
    _g("first-post", "getting-started", "Your first post from start to finish",
       "Set up your voice, write a post, approve it and publish.",
       "Start by finishing your brand voice setup. Then create a post from the Home page: pick the platforms and describe what you want to say. When it is ready, open it in Review, make any edits, and approve it. Once it is approved you can publish now or schedule it for later.",
       "start begin tutorial first"),
    _g("where-things-are", "getting-started", "Where to find things",
       "A quick tour of the main pages in the menu.",
       "Home shows what is running and your campaigns. Review is where new posts wait for approval. Library holds everything saved. Calendar shows what is scheduled. Performance shows how posts did. Activity Log lists what needs your decision and what has been done. Support is where you can find help and file a ticket.",
       "menu navigation pages home review library calendar"),
    _g("approval-first", "getting-started", "Nothing publishes until you approve it",
       "Every new piece waits for approval before it can be scheduled or published.",
       "Recast never posts on its own without approval. New pieces land as waiting for approval. Someone with permission to approve opens the piece in Review and approves it. Only then can it be scheduled or published.",
       "approve approval review permission pending"),

    # ── Brand voice ──────────────────────────────────────────────────────────
    _g("setup-brand-voice", "brand-voice", "Setting up your brand voice",
       "Finish the brand voice setup so every post sounds like you, not a generic template.",
       "The brand voice setup asks about who you are, who you write for and how you like to sound. You can start from a website or fill it in by hand. The more you complete, the closer every post gets to your own voice.",
       "brand voice onboarding setup tone identity"),
    _g("voice-on-off", "brand-voice", "Turning a brand voice off without deleting it",
       "My Voices has a real on and off switch, separate from which voice is the default.",
       "Open My Voices and use the switch on a voice to turn it off. Turning it off keeps everything you set up. It is not the same as choosing a default voice, which decides the voice used when you do not pick one.",
       "voice disable enable switch default"),
    _g("default-voice", "brand-voice", "Choosing which voice is used by default",
       "Set a default voice and default tone for each brand voice.",
       "In My Voices you can pick a default voice and a default tone for each one. New posts use these unless you choose something else while writing.",
       "default tone voice choose"),
    _g("check-my-voice", "brand-voice", "Checking a draft against your usual voice",
       "Remy compares what you wrote with how you normally sound and suggests changes.",
       "On the Remy page, paste a draft under Check My Voice and choose the platform. Remy tells you whether it sounds like you and offers openers and a rewrite idea. It only suggests. Nothing changes until you decide.",
       "remy check voice drift rewrite"),
    _g("banned-approved-words", "brand-voice", "Words to always use or never use",
       "Keep certain words out of your posts and encourage others.",
       "You can list words that should never appear and words you like to use. Posts are checked against the never list, and the always list is passed to the writer as vocabulary to prefer.",
       "banned words jargon vocabulary blacklist whitelist"),

    # ── Writing posts ────────────────────────────────────────────────────────
    _g("repurpose", "writing", "Turning one idea into posts for several platforms",
       "Write once and adapt it for each platform's style and limits.",
       "Choose several platforms when you create a post, or repurpose a piece you already have. Recast adapts the wording to each platform and applies your voice again, so a LinkedIn post and a short post for another platform each read naturally.",
       "repurpose adapt platforms multiple"),
    _g("refine-chips", "writing", "Improving a post with one click",
       "Use the quick actions to make a post punchier, shorter or add a call to action.",
       "Open a piece and use the quick refinement buttons, such as making it punchier, shortening it or adding a call to action. You can also type your own instruction. Each change is saved as a version, so you can go back.",
       "refine edit chips punchier shorten version"),
    _g("angles", "writing", "Trying different angles",
       "See three genuinely different ways to say the same thing.",
       "Ask for angles and Recast writes three different approaches to the same idea, not just tone changes. Pick the one you like and keep editing from there.",
       "angles variants rewrite ideas"),
    _g("hook-score", "writing", "Scoring your opening line",
       "Get a score for a hook and three alternatives.",
       "Paste an opening line into the hook scorer. You get a score and three alternative openings to compare. It is a tool you choose to use, and it never changes your post on its own.",
       "hook opening score first line"),
    _g("flagged-for-review", "writing", "Why a post was flagged for review",
       "Some posts are held back when they break a rule you set.",
       "Posts are checked for things like words you banned, platform length limits and generic openings. If a post still fails after an automatic second try, it is flagged so a person looks at it before it goes any further.",
       "flagged quality check banned length review"),
    _g("presets", "writing", "Using presets",
       "Save a way of writing and reuse it.",
       "Presets store a structure or style you use often. Pick one when you create a post and Recast follows it.",
       "preset template structure reuse"),

    # ── Audio and images ─────────────────────────────────────────────────────
    _g("script-to-audio", "audio-image", "Turning a script into audio",
       "Write a script and have it read aloud in a voice you choose.",
       "In the Audio page, write or paste a script and choose a voice. Recast creates the recording, which then waits for approval like any other piece. You can also make a conversation with more than one voice.",
       "audio script narration voice dialogue speech"),
    _g("upload-recording", "audio-image", "Uploading or recording your own audio",
       "Upload a file or record with your microphone, then get a transcript and a cleaner sound.",
       "Upload a recording or record in the browser. Recast writes out what was said and can reduce background noise and even out the volume. You can play it back and read the transcript in the same place.",
       "upload record microphone transcript cleanup noise"),
    _g("translate-audio", "audio-image", "Translating audio into another language",
       "Create a version in another language from a script or transcript.",
       "Choose a language and Recast translates the text, checks the translation, and creates a new recording in the voice you use. Not every language is available for every voice. If one is not, you are told why before anything is created.",
       "localize translate language tamil audio"),
    _g("share-link", "audio-image", "Sharing audio or images with a link",
       "Send someone a link to a piece without giving them access to your workspace.",
       "Audio and images can be shared with a link that stops working on a set date. Anyone with the link can view it, so share it only with people you trust. You can turn the link off any time.",
       "share link public expires"),
    _g("images", "audio-image", "Making images and carousels",
       "Create images from a prompt or upload your own, in the sizes each platform needs.",
       "Pick a layout, add your text and logo, and choose a font. You can build carousels with several slides, reorder them, and approve the result. Finished images can be exported as PNG or WebP.",
       "image carousel layout logo font export png"),

    # ── Campaigns ────────────────────────────────────────────────────────────
    _g("what-is-campaign", "campaigns", "What a campaign is",
       "A topic, the platforms you want, and how often to post.",
       "A campaign turns one topic into a run of posts over several days. Recast plans a different angle for each day, writes the posts in your voice, and puts them in Review. You approve them and they publish on schedule.",
       "campaign cadence schedule days topic"),
    _g("setup-campaign", "campaigns", "Setting up a campaign",
       "Choose a topic, platforms, how often, and an optional thumbnail.",
       "Start a campaign from Home. The platform list starts from the accounts you have connected, and you can select or deselect any of them. Recast can suggest more topic ideas. Each campaign has its own settings page.",
       "create campaign wizard platforms thumbnail suggestions"),
    _g("campaign-behind", "campaigns", "Why a campaign is behind or slowed down",
       "Recast slows down a campaign that keeps failing and tells you.",
       "If a campaign keeps failing, Recast waits longer between tries instead of retrying every minute, and records it in the Activity Log. A workspace owner can also pause new generation for everyone, which pauses campaign writing too.",
       "campaign stalled behind paused failing backoff"),
    _g("calendar-campaign", "campaigns", "Seeing one campaign on the calendar",
       "Click a campaign badge to show only its posts.",
       "On the Calendar, each post shows the campaign it belongs to. Click the badge to filter the whole calendar to that campaign, and click it again to clear the filter.",
       "calendar filter campaign badge"),

    # ── Publishing ───────────────────────────────────────────────────────────
    _g("publish-now-or-later", "publishing", "Publishing now or scheduling for later",
       "Approved posts can go out right away or at a time you choose.",
       "Once a post is approved you can publish it now or pick a date and time. If a platform has a short problem, Recast tries again by itself for a while before it marks the post as failed.",
       "publish schedule retry now later time"),
    _g("post-failed", "publishing", "My post says publishing or failed",
       "What to check when a post does not go out.",
       "First check that the account is still connected in Settings. If it says the account needs reconnecting, reconnect it and try again. If the post stays stuck, use Report a problem on the failed post so we can see exactly what happened.",
       "failed stuck publishing error retry report"),
    _g("paused-odette", "publishing", "What \"paused\" means on the Odette dashboard",
       "A workspace owner can pause new generation for the whole team.",
       "When the owner pauses generation, no new text, audio or image work starts until it is turned back on. Posts that are already scheduled are not affected and still go out.",
       "paused kill switch odette stop generation owner"),
    _g("which-platforms", "publishing", "Which platforms you can publish to",
       "LinkedIn, Facebook, Instagram, Threads, Bluesky and YouTube.",
       "You can connect and publish to LinkedIn, Facebook, Instagram, Threads, Bluesky and YouTube today. Other platforms appear in the list as coming later and cannot be published to yet.",
       "platforms supported linkedin facebook instagram threads bluesky youtube"),
    _g("youtube-publish", "publishing", "Publishing a video to YouTube",
       "Review the title, tags and chapters before it goes out.",
       "Attach a video to a piece and Recast reads it to suggest a title, tags and chapters based on what is really said. You can edit all of it before you publish. Captions need you to reconnect YouTube once so the extra permission can be added.",
       "youtube video chapters captions title tags"),

    # ── Connected accounts ───────────────────────────────────────────────────
    _g("connect-account", "accounts", "Connecting an account",
       "Link your social accounts from Settings.",
       "Open Settings, go to Connections and choose a platform. A small window opens where you sign in to that platform and allow access. When it closes, the account shows as connected.",
       "connect account settings connections sign in permission"),
    _g("reconnect-account", "accounts", "When an account needs reconnecting",
       "Access can expire or be removed, and reconnecting takes a minute.",
       "Recast renews access automatically before it expires. If a renewal keeps failing, or you removed access on the platform, the account shows as needing to be reconnected. Reconnect it from Settings and posting carries on.",
       "reconnect expired token disconnected renew"),
    _g("linkedin-followers", "accounts", "Why my LinkedIn follower count shows 0",
       "LinkedIn does not share follower counts for personal accounts.",
       "LinkedIn's own rules for personal accounts do not include follower counts, so Recast cannot show them. Posting and your profile details still work normally.",
       "linkedin followers count zero"),
    _g("threads-insights", "accounts", "Threads insights say I need to reconnect",
       "One extra permission was added, so reconnect once.",
       "Threads insights need one extra permission that was added after some accounts first connected. Disconnect and reconnect the account once and the insights will appear.",
       "threads insights reconnect permission"),
    _g("youtube-analytics", "accounts", "YouTube analytics show nothing",
       "New YouTube connections are held behind Google's own review.",
       "Google reviews apps before they can read YouTube analytics. Until that review is approved, analytics can be empty even though posting works. A connected account with no YouTube channel also has nothing to show.",
       "youtube analytics empty review google"),

    # ── Team and settings ────────────────────────────────────────────────────
    _g("roles", "team", "What each role can do",
       "Owner, admin, editor and viewer.",
       "Owners can do everything, including managing roles and the workspace settings. Admins can invite and remove people, edit the brand voice, manage connections and approve content. Editors can create, edit and publish content. Viewers can look but not change anything.",
       "roles permissions owner admin editor viewer"),
    _g("invite-team", "team", "Inviting teammates",
       "Send an invite by email and choose their role.",
       "Owners and admins can invite people by email and pick a role. Each workspace has a limited number of seats, so you may need to remove someone before inviting another person.",
       "invite team members seats email"),
    _g("library-export", "team", "Exporting your library",
       "Library export is not available yet.",
       "You cannot export your whole library yet. Bulk export is on the list of things still to come. If you need your content out today, file a ticket and we will help.",
       "export library csv markdown zip download"),

    # ── Help and safety ──────────────────────────────────────────────────────
    _g("file-ticket", "help", "Filing and following a support ticket",
       "Tell us what is wrong and follow the replies in one place.",
       "Use the File a ticket form on this page. You can attach screenshots, and we pick up which page you were on. We reply on the ticket and by email. You can add more details, close it when it is sorted, or withdraw it if you no longer need help.",
       "ticket support help file reply attach"),
    _g("support-data-privacy", "help", "How long we keep your support data",
       "Closed tickets are cleaned after about two years, and you can delete yours any time.",
       "We keep the words and files of a ticket for about 24 months after it closes. After that they are removed and only counts stay, such as the area and how long it took. You can delete all of your support tickets, files and chats yourself from the Support page. Deleting is permanent, and any ticket that is still open is closed first.",
       "privacy delete data erase retention gdpr remove tickets chats"),
    _g("remy-odette", "help", "What Remy and Odette do",
       "Remy coaches your own voice. Odette keeps an eye on the whole workspace.",
       "Remy is your personal assistant. It learns how you write and points out when a draft drifts. Odette looks after the workspace: it raises flags such as going over your seats and offers insights from recent activity. Both only suggest, and you decide what to do.",
       "remy odette assistant supervisor flags insights"),
    _g("ai-limit", "help", "The monthly AI usage limit",
       "A workspace can have a usage cap for AI writing.",
       "A workspace owner can set how much AI writing the workspace may use in a month. When the cap is reached, new writing waits until usage drops back under it. Image and voice creation are not counted in this cap.",
       "usage limit budget tokens cap ai"),
    _g("two-step", "help", "Two-step sign-in and session controls",
       "These are not available yet.",
       "You sign in with a one-time code sent to your email or phone. Extra protection such as two-step sign-in and a list of signed-in devices is not available yet.",
       "security 2fa two step sessions sign in password"),
    _g("billing-plans", "help", "Billing and plans",
       "Billing is not available yet.",
       "There is no billing or plan management screen yet. If you have a question about plans or seats, file a ticket and we will answer it directly.",
       "billing plan payment subscription price seats"),
    _g("video-status", "help", "Video",
       "Video creation is not ready yet.",
       "Recast cannot create videos yet. It can read a video you upload, write out what is said, and prepare a title, tags, chapters and captions for YouTube.",
       "video pipeline create render"),
]

_WORD = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 2]


def search(query: str, limit: int = 3) -> list[dict]:
    """The guides that best match ``query``. Simple keyword scoring; the
    corpus is small, so nothing fancier is needed."""
    words = set(_tokens(query))
    if not words:
        return []
    scored: list[tuple[int, dict]] = []
    for g in GUIDES:
        haystack = f"{g['title']} {g['summary']} {g['keywords']} {g['body']}".lower()
        score = sum(3 if w in g["title"].lower() else 2 if w in g["keywords"].lower() else 1 for w in words if w in haystack)
        if score:
            scored.append((score, g))
    scored.sort(key=lambda x: -x[0])
    return [g for _, g in scored[:limit]]
