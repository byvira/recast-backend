"""Tools for publishing: suggested times, a post's publishing status, YouTube and Instagram helpers, sending to a destination, platform
details and runs (background work). Scheduling and approving a post are in the Library tools.

Connecting an account or saving a platform's own webhook address needs the person's own sign-in or secret, so those routes stay in the
app (see exemptions.py)."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

PB = "/api/v1/publish"
DS = "/api/v1/destinations"
PF = "/api/v1/platforms"
RN = "/api/v1/runs"


def rows() -> list[T]:
    return [
        T("publish.suggest_times", "Suggest up to three good times to publish a post, in the person's time zone.", Risk.READ, (f"GET {PB}/suggest-times",),
          examples=("When is a good time to post this?",)),
        T("publish.status", "Show whether a post has gone out, is waiting, or failed, and why.", Risk.READ, (f"GET {PB}/{{piece_id}}/status",),
          examples=("Did my post go out?",)),
        T("publish.youtube_playlists", "List the connected YouTube channel's playlists.", Risk.READ, (f"GET {PB}/youtube/playlists",),
          examples=("Which playlists do I have on YouTube?",)),
        T("publish.youtube_new_playlist", "Make a new YouTube playlist on the connected channel (private by default).", Risk.WRITE,
          (f"POST {PB}/youtube/playlists",), note="Free.", undo="Delete the playlist on YouTube; Ekavo cannot remove it.",
          examples=("Make a playlist called Launch week",)),
        T("publish.youtube_prepare", "Write a YouTube title, description, tags and category for a post, in the brand's voice.", Risk.WRITE,
          (f"POST {PB}/youtube/prepare",), note="Free: part of getting a post ready.", examples=("Prepare the YouTube details for this video",)),
        T("publish.instagram_locations", "Search places by name, for the location of an Instagram post.", Risk.READ, (f"GET {PB}/instagram/locations",),
          examples=("Find the location Marina Beach for Instagram",)),
        T("publish.send_to_destination", "Send a blog or newsletter post to the destination chosen on it (WordPress, Ghost or Mailchimp).",
          Risk.PUBLISH, (f"POST {DS}/send",), undo="Once it has gone out it cannot be taken back by Ekavo.", examples=("Send this to my WordPress site",),
          human_fields=("confirm_publish_anyway", "confirm_send")),
        T("destinations.mailchimp_audiences", "List the Mailchimp audiences a newsletter can go to.", Risk.READ, (f"GET {DS}/mailchimp/audiences",),
          examples=("Which Mailchimp lists can I send to?",)),
        T("destinations.wordpress_choices", "List the categories and authors a WordPress post can use.", Risk.READ, (f"GET {DS}/wordpress/choices",),
          examples=("Which WordPress categories can I use?",)),
        # platforms
        T("platforms.list", "List the platforms Ekavo works with and what each can do.", Risk.READ, (f"GET {PF}",),
          examples=("Which platforms can I publish to?",)),
        T("platforms.get", "Show one platform: what it can do, its limits and its status.", Risk.READ, (f"GET {PF}/{{key}}",)),
        T("platforms.listing", "Show this workspace's listing for a podcast directory, or nothing when there is none yet.", Risk.READ,
          (f"GET {PF}/{{key}}/listing",), examples=("Is my podcast listed on Spotify?",)),
        T("platforms.record_listing", "Record the link a member got from a podcast directory and where the listing stands. Nothing checks it.",
          Risk.WRITE, (f"PUT {PF}/{{key}}/listing",), note="Free.", undo="Record it again with new details."),
        # runs: background work
        T("runs.get", "Show one run: what it is, how far along, and its result.", Risk.READ, (f"GET {RN}/{{run_id}}",),
          examples=("How far along is that video?",)),
        T("runs.retry", "Start a failed run again, with the same settings. Only work that is safe to repeat can be retried.", Risk.WRITE,
          (f"POST {RN}/{{run_id}}/retry",), note="Charged the same as the original run, and only for what is made.",
          examples=("Try that failed run again",)),
        T("runs.control", "Pause, resume or cancel a run. Only the person who started it, or an admin, can.", Risk.WRITE,
          (f"POST {RN}/{{run_id}}/{{action}}",), note="Free. What was already made is kept.", undo="Resume a paused run with the same tool.",
          examples=("Stop that render", "Pause my batch")),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
