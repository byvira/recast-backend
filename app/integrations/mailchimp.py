"""Mailchimp integration client."""

from typing import Any


class MailchimpClient:
    """Placeholder client for the Mailchimp Marketing API."""

    async def connect(self, credentials: dict[str, Any]) -> None:
        """Authenticate with Mailchimp using an API key and data centre prefix.

        Args:
            credentials: Dict containing api_key and data_center (e.g. "us1").
        """
        # Placeholder: validate API key and store the data centre prefix
        pass

    async def publish(self, content: dict[str, Any]) -> str:
        """Create and send a Mailchimp campaign.

        Args:
            content: Dict with subject, html_body, list_id, and from_name.

        Returns:
            Mailchimp campaign ID of the sent campaign.
        """
        # Placeholder: create campaign, set content, then trigger send — return ID
        return "mc_campaign_placeholder_id"

    async def get_status(self, post_id: str) -> dict[str, Any]:
        """Retrieve send report metrics for a Mailchimp campaign.

        Args:
            post_id: Campaign ID returned by :meth:`publish`.

        Returns:
            Dict with status, open_rate, click_rate, and unsubscribes fields.
        """
        # Placeholder: call GET /reports/{campaign_id}
        return {"post_id": post_id, "status": "sent"}
