"""Where the command line keeps what it needs between runs: the server address and the sign-in token.

The file is private to the person (mode 0600 where the system has modes). `EKAVO_TOKEN` and `EKAVO_API_URL` override it, for a script or a build
machine that signs in another way. The token is never printed."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Settings:
    url: str = ""
    token: str = ""
    workspace_id: str = ""
    scopes: tuple[str, ...] = ()
    expires_at: str = ""


def path() -> Path:
    base = os.environ.get("EKAVO_CONFIG_DIR")
    if base:
        return Path(base) / "credentials.json"
    root = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root or Path.home()) / "ekavo" / "credentials.json"


def load() -> Settings:
    saved = Settings()
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        saved = Settings(url=str(data.get("url") or ""), token=str(data.get("token") or ""), workspace_id=str(data.get("workspace_id") or ""),
                         scopes=tuple(data.get("scopes") or ()), expires_at=str(data.get("expires_at") or ""))
    except (OSError, ValueError):
        pass
    return Settings(url=os.environ.get("EKAVO_API_URL") or saved.url, token=os.environ.get("EKAVO_TOKEN") or saved.token, workspace_id=saved.workspace_id,
                    scopes=saved.scopes, expires_at=saved.expires_at)


def save(settings: Settings) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"url": settings.url, "token": settings.token, "workspace_id": settings.workspace_id, "scopes": list(settings.scopes), "expires_at": settings.expires_at}), encoding="utf-8")
    try:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def forget() -> bool:
    try:
        path().unlink()
        return True
    except OSError:
        return False


def mask(token: Optional[str]) -> str:
    return f"{token[:12]}…" if token else ""
