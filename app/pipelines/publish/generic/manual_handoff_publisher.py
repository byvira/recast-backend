"""Generic publisher for manual_handoff platforms (X, Reddit, Hacker News, Medium, Substack, ...).

It never posts. It builds a prefilled compose link and hands it back for the member to open and press Post
themselves, so there is no way to know it was posted. That is why the result is always success=False with a
manual_action_url: reporting success would be untrue, since nothing was posted. The member confirms with
"I posted this myself".

Settings (platform config fields): `compose_url_template` (required, https, placeholders {text}, {content},
{title}, {url} are URL-encoded) and `instructions` (plain text shown to the member next to the link).
"""

import logging
from urllib.parse import quote

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.generic.safe_url import UnsafeUrl, check_https_url

logger = logging.getLogger(__name__)

_PLACEHOLDERS = ("{text}", "{content}", "{title}", "{url}")


def template_problem(template: str) -> str | None:
    """A plain reason the compose template cannot be used, or None when it is fine."""
    if not (template or "").strip():
        return "No compose link is set for this platform yet."
    probe = template
    for token in _PLACEHOLDERS:
        probe = probe.replace(token, "x")
    try:
        check_https_url(probe)
    except UnsafeUrl as exc:
        return str(exc)
    if not any(token in template for token in _PLACEHOLDERS):
        return "The compose link needs {text} so the post can be filled in."
    return None


def build_compose_url(template: str, content: str, link: str = "") -> str:
    title = (content.strip().splitlines() or [""])[0][:100]
    url = template
    for token, value in (("{text}", content), ("{content}", content), ("{title}", title), ("{url}", link)):
        url = url.replace(token, quote(value, safe=""))
    return url


class ManualHandoffPublisher:
    async def publish(
        self,
        workspace_id: str,
        platform: str,
        content: str,
        fields: dict,
        piece_id: str = "",
    ) -> PublishResult:
        template = str(fields.get("compose_url_template") or "")
        problem = template_problem(template)
        if problem:
            return PublishResult(
                success=False, platform=platform, piece_id=piece_id,
                error_type="FIXABLE", error_message=problem,
            )

        logger.info("Manual handoff link built, workspace=%s platform=%s", workspace_id, platform)
        # success=False is deliberate: nothing has been posted, only a link handed back.
        return PublishResult(
            success=False,
            platform=platform,
            piece_id=piece_id,
            manual_action_url=build_compose_url(template, content),
            manual_instructions=str(fields.get("instructions") or "") or None,
        )
