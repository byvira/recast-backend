"""The Ekavo command line, against a stand-in for the server: sign-in, the saved token, running tools, prices, retries, exit codes. No network."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))

from ekavo_cli import config  # noqa: E402
from ekavo_cli.client import ApiError, Client, Response  # noqa: E402
from ekavo_cli.main import FAILED, NEEDS_YES, NOT_SIGNED_IN, OK, USAGE, main  # noqa: E402


@pytest.fixture(autouse=True)
def _private_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EKAVO_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("EKAVO_TOKEN", raising=False)
    monkeypatch.delenv("EKAVO_API_URL", raising=False)


class Server:
    """Answers like the real server, and remembers what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, method, url, headers, body):
        self.seen.append((method, url.split("/api/public/v1")[-1], headers, body))
        reply = self.replies.pop(0) if self.replies else Response(200, {}, {})
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(argv, server, sleep=lambda s: None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, transport=server, sleep=sleep)
    return code, out.getvalue(), err.getvalue()


def ok(body):
    return Response(200, body, {})


def refused(status, code, message, **extra):
    return Response(status, {"error": {"code": code, "message": message, **extra}}, {})


def sign_in(token="ek_live_secret_token_value"):
    config.save(config.Settings(url="https://api.test", token=token, workspace_id="ws1", scopes=("read", "create"), expires_at="2027-02-01T00:00:00Z"))


# ── signing in ───────────────────────────────────────────────────────────────

def test_login_shows_the_code_waits_for_the_yes_and_keeps_the_token_privately():
    server = Server([
        Response(201, {"device_code": "d" * 20, "user_code": "ABCD-2345", "verification_url": "https://app.test/device", "expires_in": 600, "interval": 3}, {}),
        ok({"status": "pending", "interval": 3}),
        ok({"status": "slow_down", "interval": 5}),
        ok({"status": "approved", "access_token": "ek_live_abc123", "workspace_id": "ws1", "scopes": ["read", "create"], "expires_at": "2027-02-10T00:00:00Z"}),
    ])
    code, out, err = run(["login", "--url", "https://api.test/", "--name", "Work laptop", "--no-browser"], server)
    assert code == OK and "ABCD-2345" in out and "https://app.test/device" in out and "Signed in" in out and "2027-02-10" in out
    assert server.seen[0][1] == "/device/start" and server.seen[0][3] == {"client": "cli", "label": "Work laptop"}
    saved = config.load()
    assert saved.token == "ek_live_abc123" and saved.url == "https://api.test" and saved.scopes == ("read", "create")
    assert "ek_live_abc123" not in out + err                                         # the token is never printed
    if os.name != "nt":
        assert oct(config.path().stat().st_mode & 0o777) == "0o600"


def test_login_without_an_address_says_so_and_a_denied_sign_in_is_a_plain_failure():
    code, out, err = run(["login", "--no-browser"], Server([]))
    assert code == USAGE and "--url" in err
    server = Server([Response(201, {"device_code": "d" * 20, "user_code": "ABCD-2345", "verification_url": "https://app.test/device", "expires_in": 600, "interval": 1}, {}),
                     refused(403, "denied", "You said no on the sign-in screen, so nothing was connected.")])
    code, out, err = run(["login", "--url", "https://api.test", "--no-browser"], server)
    assert code == FAILED and "nothing was connected" in err and config.load().token == ""


def test_logout_revokes_the_session_then_forgets_it_and_says_so_when_the_server_cannot_be_reached():
    sign_in()
    server = Server([ok({"ok": True})])
    code, out, err = run(["logout"], server)
    assert code == OK and server.seen[0][1] == "/device/revoke" and config.load().token == ""
    sign_in()
    code, out, err = run(["logout"], Server([ApiError(0, "network_error", "down", retryable=True)]))
    assert code == OK and "Settings, Connections" in out and config.load().token == ""


def test_whoami_shows_where_you_are_signed_in_without_the_token_and_not_signed_in_is_exit_4():
    code, out, err = run(["whoami"], Server([]))
    assert code == NOT_SIGNED_IN
    sign_in("ek_live_veryverysecret")
    code, out, err = run(["whoami"], Server([]))
    assert code == OK and "ws1" in out and "read, create" in out and "veryverysecret" not in out
    for argv in (["tools"], ["describe", "plan.limits"], ["call", "plan.limits"]):
        config.forget()
        assert run(argv, Server([]))[0] == NOT_SIGNED_IN


# ── running tools ────────────────────────────────────────────────────────────

def test_tools_lists_what_you_can_do_and_a_search_is_sent_to_the_server():
    sign_in()
    server = Server([ok({"tools": [{"name": "plan.limits", "summary": "Show the plan's limits.", "risk": "read", "cost": "free"}], "count": 1})])
    code, out, err = run(["tools", "--search", "limit"], server)
    assert code == OK and "plan.limits" in out and "1 tools" in out and server.seen[0][1] == "/tools?q=limit"
    assert server.seen[0][2]["Authorization"] == "Bearer ek_live_secret_token_value"


def test_call_takes_inputs_as_key_value_or_json_shows_a_dry_run_price_and_prints_the_data():
    sign_in()
    server = Server([ok({"ok": True, "dry_run": True, "estimate": {"credits": 7, "exact": False}})])
    code, out, err = run(["call", "library.create_draft", "--arg", "platform=LinkedIn", "--arg", "count=3", "--arg", 'tags=["a","b"]', "--json", '{"content": "Hi"}', "--dry-run"], server)
    assert code == OK and "Nothing was done" in out and "7 credits or more" in out
    sent = server.seen[0][3]
    assert sent == {"arguments": {"content": "Hi", "platform": "LinkedIn", "count": 3, "tags": ["a", "b"]}, "dry_run": True}
    server = Server([ok({"ok": True, "data": {"plan": "Free"}, "warnings": [{"code": "degraded", "message": "No AI picture."}]})])
    code, out, err = run(["call", "plan.limits"], server)
    assert code == OK and json.loads(out) == {"plan": "Free"} and "No AI picture." in err


def test_a_bad_input_is_a_usage_error_before_anything_is_sent():
    sign_in()
    server = Server([])
    for argv in (["call", "x", "--arg", "nonsense"], ["call", "x", "--json", "[1]"]):
        assert run(argv, server)[0] == USAGE
    assert server.seen == []


def test_a_refusal_is_said_in_plain_words_with_the_code_and_what_to_do_and_a_price_asks_for_confirmation():
    sign_in()
    code, out, err = run(["call", "image.generate"], Server([refused(402, "credits_exhausted", "You are out of credits.", retryable=False, next="Add credits in Settings.")]))
    assert code == FAILED and "out of credits" in err and "credits_exhausted" in err and "Add credits" in err
    code, out, err = run(["call", "image.generate"], Server([refused(409, "confirm_required", "This costs 24 credits. Repeat the call with confirmed_credits 24.", estimate=24)]))
    assert code == NEEDS_YES and "--confirmed-credits 24" in err
    code, out, err = run(["call", "image.generate", "--output-json"], Server([refused(401, "auth_invalid", "A valid sign-in is needed.")]))
    assert code == NOT_SIGNED_IN and json.loads(err)["error"]["code"] == "auth_invalid"


def test_a_read_is_tried_again_when_the_server_says_it_can_help_but_a_call_that_makes_something_never_is():
    waits = []
    busy = refused(503, "ai_busy", "The writing service is busy.", retryable=True, retry_after_seconds=5)
    reads = Server([busy, ok({"tools": [], "count": 0})])
    client = Client("https://api.test", "tok", transport=reads, sleep=waits.append)
    assert client.tools() == [] and len(reads.seen) == 2 and waits == [5]

    writes = Server([busy, ok({"ok": True})])
    client = Client("https://api.test", "tok", transport=writes, sleep=waits.append)
    with pytest.raises(ApiError) as caught:
        client.call("library.create_draft", {})
    assert caught.value.code == "ai_busy" and caught.value.retryable and len(writes.seen) == 1       # one attempt: it could have been made

    long_wait = Server([refused(503, "ai_busy", "busy", retryable=True, retry_after_seconds=300), ok({"tools": []})])
    with pytest.raises(ApiError):
        Client("https://api.test", "tok", transport=long_wait, sleep=waits.append).tools()
    assert len(long_wait.seen) == 1                                                                    # a long wait is the person's to make, not the command's


def test_an_idempotency_key_and_an_approval_token_are_sent_and_the_environment_can_stand_in_for_the_saved_sign_in(monkeypatch):
    monkeypatch.setenv("EKAVO_API_URL", "https://ci.test")
    monkeypatch.setenv("EKAVO_TOKEN", "ek_live_from_ci")
    server = Server([ok({"ok": True, "data": {}})])
    code, out, err = run(["call", "audio.cleanup", "--idempotency-key", "run-42", "--approval-token", "once", "--confirmed-credits", "30"], server)
    assert code == OK
    method, path, headers, body = server.seen[0]
    assert headers["Idempotency-Key"] == "run-42" and headers["Authorization"] == "Bearer ek_live_from_ci" and body["approval_token"] == "once" and body["confirmed_credits"] == 30


def test_token_prints_the_token_and_the_mcp_address_only_when_asked_and_warns_to_keep_it_private():
    code, out, err = run(["token"], Server([]))
    assert code == NOT_SIGNED_IN
    sign_in("ek_live_for_the_assistant")
    code, out, err = run(["token", "--mcp"], Server([]))
    assert code == OK and "https://api.test/api/public/v1/mcp" in out and out.strip().endswith("ek_live_for_the_assistant") and "Keep this private" in err
    code, out, err = run(["whoami"], Server([]))
    assert "for_the_assistant" not in out                                          # nothing else ever prints it
