"""Generic publisher for manual_handoff platforms (X for now, Reddit for now,
Hacker News, Medium, Substack, ...). Never actually posts — builds a prefilled
compose link and hands it back for the user to open and click "Post"
themselves. No publish tracking is possible here, by the source directory's
own definition of this pattern — see app/pipelines/publish/generic/__init__.py.
"""

import logging
from urllib.parse import quote

from app.pipelines.publish.base import PublishResult

logger = logging.getLogger(__name__)


class ManualHandoffPublisher:
    async def publish(
        self,
        workspace_id: str,
        platform: str,
        content: str,
        fields: dict,
        piece_id: str = "",
    ) -> PublishResult:
        template = fields.get("compose_url_template", "")
        if not template:
            return PublishResult(
                success=False,
                platform=platform,
                piece_id=piece_id,
                error_type="FIXABLE",
                error_message="No compose_url_template configured for this platform in the Ops Dashboard.",
            )

        compose_url = template.replace("{content}", quote(content))
        logger.info("Manual handoff link built — workspace=%s platform=%s", workspace_id, platform)

        # success=False is deliberate — nothing has been posted, only a link
        # handed back. See PublishResult.manual_action_url's docstring.
        return PublishResult(
            success=False,
            platform=platform,
            piece_id=piece_id,
            manual_action_url=compose_url,
        )
