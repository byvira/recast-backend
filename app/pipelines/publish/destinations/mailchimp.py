"""Mailchimp through its Marketing API, signed in with the member's API key (the part after the dash names their data centre). A
newsletter is made as a campaign for one audience. By default it is left as a draft in Mailchimp for the member to look over and send;
it goes to the audience only when sending is chosen and confirmed. Built from Mailchimp's documented API; not yet confirmed against a
live account.
"""
from __future__ import annotations

import base64
import re
from typing import Optional

import httpx

from app.pipelines.publish.base import PublishResult
from app.pipelines.publish.destinations.common import DestinationError, TIMEOUT, failure, reply_message, to_html

LABEL = "Mailchimp"

_KEY = re.compile(r"^[0-9a-f]{20,40}-([a-z]{2,3}\d{1,3})$", re.IGNORECASE)

#: Mailchimp refuses to send a campaign that has no way to unsubscribe and no postal address, so both are in every footer.
FOOTER = (
    '<hr><p style="font-size:12px;color:#666">You are receiving this because you subscribed. '
    '<a href="*|UNSUB|*">Unsubscribe</a><br>*|LIST:ADDRESSLINE|*</p>'
)


def data_centre(api_key: str) -> str:
    match = _KEY.match((api_key or "").strip())
    if not match:
        raise DestinationError("That does not look like a Mailchimp API key. It ends with a dash and a short code such as us21.")
    return match.group(1).lower()


def _base(api_key: str) -> str:
    return f"https://{data_centre(api_key)}.api.mailchimp.com/3.0"


def _headers(api_key: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"recast:{api_key.strip()}".encode()).decode(), "Accept": "application/json"}


async def verify(api_key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.get(f"{_base(api_key)}/", params={"fields": "account_name,login_id"}, headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise DestinationError("Mailchimp could not be reached. Try again.") from exc
    if response.status_code in (401, 403):
        raise DestinationError("Mailchimp did not accept that API key.")
    if response.status_code != 200:
        raise DestinationError("Mailchimp could not confirm the key. Try again.")
    data = response.json()
    return {"data_centre": data_centre(api_key), "name": data.get("account_name") or "Mailchimp", "login_id": str(data.get("login_id", ""))}


async def audiences(api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            f"{_base(api_key)}/lists", params={"count": 100, "fields": "lists.id,lists.name,lists.stats.member_count"}, headers=_headers(api_key),
        )
    if response.status_code != 200:
        return []
    return [{"id": a["id"], "name": a["name"], "members": (a.get("stats") or {}).get("member_count", 0)} for a in response.json().get("lists", [])]


def build_html(content: str) -> str:
    return f"<!doctype html><html><body>{to_html(content)}{FOOTER}</body></html>"


async def publish(*, api_key: str, piece_id: str, audience_id: str, subject: str, preview: str, content: str, send: bool) -> PublishResult:
    base, headers = _base(api_key), _headers(api_key)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            audience = await client.get(f"{base}/lists/{audience_id}", params={"fields": "name,campaign_defaults"}, headers=headers)
            if audience.status_code != 200:
                return failure("mailchimp", piece_id, audience, message="That audience could not be found in Mailchimp.", reconnect_label=LABEL)
            defaults = audience.json().get("campaign_defaults") or {}
            if not defaults.get("from_email"):
                return PublishResult(success=False, platform="mailchimp", piece_id=piece_id, error_type="FATAL", error_code=422,
                                     error_message="The audience has no sender email set. Add one in Mailchimp under the audience's settings.")
            campaign = await client.post(f"{base}/campaigns", headers=headers, json={
                "type": "regular",
                "recipients": {"list_id": audience_id},
                "settings": {
                    "subject_line": subject[:150], "preview_text": preview[:150], "title": subject[:100],
                    "from_name": defaults.get("from_name") or audience.json().get("name", ""), "reply_to": defaults["from_email"],
                },
            })
            if campaign.status_code not in (200, 201):
                return failure("mailchimp", piece_id, campaign, message=reply_message(campaign), reconnect_label=LABEL)
            created = campaign.json()
            content_reply = await client.put(f"{base}/campaigns/{created['id']}/content", json={"html": build_html(content)}, headers=headers)
            if content_reply.status_code != 200:
                return failure("mailchimp", piece_id, content_reply, message=reply_message(content_reply), reconnect_label=LABEL)
            if send:
                sent = await client.post(f"{base}/campaigns/{created['id']}/actions/send", headers=headers)
                if sent.status_code not in (200, 204):
                    return failure("mailchimp", piece_id, sent, message=reply_message(sent) or "Mailchimp would not send the campaign. It is saved as a draft.", reconnect_label=LABEL)
    except httpx.TimeoutException:
        return PublishResult(success=False, platform="mailchimp", piece_id=piece_id, error_type="TRANSIENT", error_code=408, error_message="Mailchimp took too long to answer. Check Mailchimp before trying again.")
    except httpx.HTTPError:
        return PublishResult(success=False, platform="mailchimp", piece_id=piece_id, error_type="FATAL", error_code=502, error_message="Mailchimp could not be reached.")
    link: Optional[str] = f"https://{data_centre(api_key)}.admin.mailchimp.com/campaigns/edit?id={created.get('web_id')}" if created.get("web_id") else None
    return PublishResult(success=True, platform="mailchimp", piece_id=piece_id, platform_post_id=created["id"], platform_post_url=link)
