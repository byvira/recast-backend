"""The Ekavo command line: sign in once in the browser, then run any tool.

    ekavo login --url https://api.example.com      sign in (a code, then yes in the browser)
    ekavo tools --search draft                      what you can do
    ekavo describe library.create_draft             what a tool needs
    ekavo call plan.limits                          run a read
    ekavo call library.create_draft --arg platform=LinkedIn --arg content="Hello" --dry-run
    ekavo token --mcp                               the MCP address and token, for an assistant app
    ekavo logout                                    sign this computer out

Exit codes: 0 done, 1 refused or failed, 2 usage, 3 needs your yes or a confirmed price, 4 not signed in."""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from typing import Any, Callable, Optional, Sequence

from ekavo_cli import config
from ekavo_cli.client import ApiError, Client, Transport, urllib_transport

OK, FAILED, USAGE, NEEDS_YES, NOT_SIGNED_IN = 0, 1, 2, 3, 4
CONFIRM_CODES = {"confirm_required", "price_changed", "approval_required", "needs_approval"}


def _print(out, text: str = "") -> None:
    out.write(text + "\n")


def _parse_value(raw: str) -> Any:
    """A `--arg` value is JSON when it looks like JSON (a number, true, a list, an object) and plain text otherwise."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.json_arguments:
        loaded = json.loads(args.json_arguments)
        if not isinstance(loaded, dict):
            raise ValueError("--json must be an object, for example '{\"platform\": \"LinkedIn\"}'.")
        out.update(loaded)
    for item in args.arg or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--arg needs key=value, not {item!r}.")
        out[key] = _parse_value(value)
    return out


def _fail(out_err, exc: ApiError, as_json: bool) -> int:
    if as_json:
        _print(out_err, json.dumps({"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "retry_after_seconds": exc.retry_after, "next": exc.next}}))
    else:
        _print(out_err, f"{exc.message} ({exc.code})")
        if exc.retryable and exc.retry_after is not None:
            _print(out_err, f"You can try again in about {exc.retry_after} seconds.")
        if exc.next:
            _print(out_err, exc.next)
    if exc.code == "auth_invalid" or exc.status == 401:
        return NOT_SIGNED_IN
    return NEEDS_YES if exc.code in CONFIRM_CODES else FAILED


def _client(settings: config.Settings, transport: Transport) -> Optional[Client]:
    return Client(settings.url, settings.token, transport=transport) if settings.url and settings.token else None


def login(args: argparse.Namespace, out, err, transport: Transport, sleep: Callable[[float], None]) -> int:
    settings = config.load()
    url = (args.url or settings.url).rstrip("/")
    if not url:
        _print(err, "Say which server to sign in to: ekavo login --url https://...  (or set EKAVO_API_URL).")
        return USAGE
    client = Client(url, transport=transport)
    try:
        begun = client.start_login(args.name or "Command line")
    except ApiError as exc:
        return _fail(err, exc, False)
    _print(out, f"To sign in, open {begun['verification_url']} and enter this code:\n\n    {begun['user_code']}\n")
    if not args.no_browser:
        try:
            webbrowser.open(begun.get("verification_url_complete") or begun["verification_url"])
        except Exception:  # noqa: BLE001 - the address above is enough
            pass
    _print(out, "Waiting for you to say yes in the browser…")
    interval = max(1, int(begun.get("interval") or 3))
    deadline = time.monotonic() + int(begun.get("expires_in") or 600)
    while time.monotonic() < deadline:
        sleep(interval)
        try:
            answer = client.poll_login(begun["device_code"])
        except ApiError as exc:
            return _fail(err, exc, False)
        if answer.get("status") == "slow_down":
            interval = max(interval, int(answer.get("interval") or interval + 2))
            continue
        if answer.get("status") == "approved":
            config.save(config.Settings(url=url, token=answer["access_token"], workspace_id=answer.get("workspace_id", ""), scopes=tuple(answer.get("scopes") or ()), expires_at=answer.get("expires_at", "")))
            _print(out, f"Signed in. This computer can {', '.join(answer.get('scopes') or [])} in your workspace until {str(answer.get('expires_at', ''))[:10]}.")
            return OK
    _print(err, "The code ran out before it was approved. Run ekavo login again.")
    return FAILED


def logout(args: argparse.Namespace, out, err, transport: Transport, sleep) -> int:
    settings = config.load()
    client = _client(settings, transport)
    note = ""
    if client:
        try:
            client.revoke_self()
        except ApiError:
            note = " The server could not be reached, so revoke it in Settings, Connections if you want it gone at once."
    config.forget()
    _print(out, f"Signed out.{note}")
    return OK


def tools(args: argparse.Namespace, out, err, transport: Transport, sleep) -> int:
    client = _client(config.load(), transport)
    if client is None:
        _print(err, "You are not signed in. Run ekavo login first.")
        return NOT_SIGNED_IN
    try:
        found = client.tools(args.group or "", args.search or "")
    except ApiError as exc:
        return _fail(err, exc, args.json)
    if args.json:
        _print(out, json.dumps(found))
        return OK
    width = max((len(t["name"]) for t in found), default=10)
    for t in found:
        _print(out, f"{t['name']:<{width}}  {t['risk']:<11} {t['cost']:<14} {t['summary']}")
    _print(out, f"\n{len(found)} tools")
    return OK


def describe(args: argparse.Namespace, out, err, transport: Transport, sleep) -> int:
    client = _client(config.load(), transport)
    if client is None:
        _print(err, "You are not signed in. Run ekavo login first.")
        return NOT_SIGNED_IN
    try:
        tool = client.tool(args.tool)
    except ApiError as exc:
        return _fail(err, exc, args.json)
    if args.json:
        _print(out, json.dumps(tool))
        return OK
    _print(out, f"{tool['name']}: {tool['summary']}\nrisk {tool['risk']}, costs {tool['cost']}, approval {tool.get('approval') or 'none'}, undo {tool.get('undo') or 'n/a'}")
    schema = tool.get("input") or {}
    required = set(schema.get("required") or [])
    for name, spec in (schema.get("properties") or {}).items():
        _print(out, f"  --arg {name}=…  {'(required) ' if name in required else ''}{spec.get('description') or spec.get('type') or ''}")
    return OK


def call(args: argparse.Namespace, out, err, transport: Transport, sleep) -> int:
    client = _client(config.load(), transport)
    if client is None:
        _print(err, "You are not signed in. Run ekavo login first.")
        return NOT_SIGNED_IN
    try:
        arguments = _arguments(args)
    except ValueError as exc:
        _print(err, str(exc))
        return USAGE
    try:
        answer = client.call(args.tool, arguments, dry_run=args.dry_run, idempotency_key=args.idempotency_key or "", confirmed_credits=args.confirmed_credits, approval_token=args.approval_token or "")
    except ApiError as exc:
        code = _fail(err, exc, args.json)
        if code == NEEDS_YES and not args.json and exc.body.get("error", {}).get("estimate") is not None:
            _print(err, f"Run it again with --confirmed-credits {exc.body['error']['estimate']} to agree to that price.")
        return code
    if args.json:
        _print(out, json.dumps(answer))
        return OK
    if answer.get("dry_run"):
        est = answer.get("estimate") or {}
        _print(out, f"Nothing was done. It would cost {est.get('credits', 0)} credits{' or more' if est.get('exact') is False else ''}.")
        return OK
    data = answer.get("data")
    _print(out, json.dumps(data, indent=2, ensure_ascii=False) if data is not None else "Done.")
    for warning in answer.get("warnings") or []:
        _print(err, f"Note: {warning.get('message')}")
    return OK


def whoami(args: argparse.Namespace, out, err, transport: Transport, sleep) -> int:
    settings = config.load()
    if not (settings.url and settings.token):
        _print(err, "You are not signed in. Run ekavo login first.")
        return NOT_SIGNED_IN
    _print(out, f"Server {settings.url}\nWorkspace {settings.workspace_id or 'unknown'}\nAllowed to {', '.join(settings.scopes) or 'read'}\nSigned in until {settings.expires_at[:10] or 'unknown'}\nToken {config.mask(settings.token)}")
    return OK


def token(args: argparse.Namespace, out, err, transport: Transport, sleep) -> int:
    """Print the sign-in token, for an assistant app that connects to the MCP address with a header. Anyone who has the token can act as you until it
    is signed out, so it goes to the screen only when asked for."""
    settings = config.load()
    if not (settings.url and settings.token):
        _print(err, "You are not signed in. Run ekavo login first.")
        return NOT_SIGNED_IN
    if args.mcp:
        _print(out, f"MCP address:  {settings.url}/api/public/v1/mcp\nHeader:       Authorization: Bearer <the token below>\n")
    _print(out, settings.token)
    _print(err, "Keep this private: anyone who has it can act as you until you run ekavo logout.")
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ekavo", description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("login", help="sign in with the browser")
    p.add_argument("--url", help="the server address, kept for next time")
    p.add_argument("--name", help="what this computer is called in Settings, Connections")
    p.add_argument("--no-browser", action="store_true", help="only show the address and code")
    p.set_defaults(run=login)
    sub.add_parser("logout", help="sign this computer out").set_defaults(run=logout)
    sub.add_parser("whoami", help="where and how you are signed in").set_defaults(run=whoami)
    p = sub.add_parser("token", help="print the sign-in token, for an assistant app that connects with a header")
    p.add_argument("--mcp", action="store_true", help="also print the MCP address and header")
    p.set_defaults(run=token)
    p = sub.add_parser("tools", help="list what you can do")
    p.add_argument("--search")
    p.add_argument("--group")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=tools)
    p = sub.add_parser("describe", help="what one tool needs")
    p.add_argument("tool")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=describe)
    p = sub.add_parser("call", help="run one tool")
    p.add_argument("tool")
    p.add_argument("--arg", action="append", metavar="KEY=VALUE", help="an input; a value that looks like JSON is read as JSON")
    p.add_argument("--json", dest="json_arguments", metavar="OBJECT", help="all the inputs as one JSON object")
    p.add_argument("--dry-run", action="store_true", help="show the price and do nothing")
    p.add_argument("--confirmed-credits", type=int, metavar="N", help="agree to a price over 20 credits")
    p.add_argument("--approval-token", help="the token you were given after a person said yes")
    p.add_argument("--idempotency-key", help="repeat the same call safely: it is made once")
    p.add_argument("--output-json", dest="json", action="store_true", help="print the whole answer as JSON")
    p.set_defaults(run=call)
    return parser


def main(argv: Optional[Sequence[str]] = None, *, out=None, err=None, transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else USAGE
    return args.run(args, out, err, transport, sleep)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
