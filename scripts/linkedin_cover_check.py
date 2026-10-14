"""Checks that LinkedIn's Videos API accepts a library video with a cover, WITHOUT creating a post.

    python scripts/linkedin_cover_check.py --workspace-name "Vira Studio's Workspace" [--media-id <video id>]

The video and its cover go through the app's own upload code (initialize, every part, thumbnail, finalize). Then the video is read
back until LinkedIn says whether it is processed and whether it kept a thumbnail. The post call is never made, so nothing appears on
the profile or page; an unattached video is not shown anywhere. No token is printed.
"""
import argparse
import asyncio
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-name", required=True)
    ap.add_argument("--media-id")
    args = ap.parse_args()

    import httpx

    from app.db.mongo import get_db
    from app.pipelines.publish import token_store
    from app.pipelines.publish.linkedin import posts_api

    db = get_db()
    ws = await db["workspaces"].find_one({"name": args.workspace_name})
    wid = ws.get("id") or str(ws["_id"])
    tokens = await token_store.get_token(wid, "linkedin")
    if not tokens:
        print("That workspace has no active LinkedIn connection.")
        return 1
    query = {"workspace_id": wid, "kind": "video", "url": {"$regex": "^https://"}}
    if args.media_id:
        query["id"] = args.media_id
    media = await db["media_assets"].find_one(query, sort=[("created_at", -1)])
    if not media:
        print("No video in the library.")
        return 1
    owner = f"urn:li:person:{tokens['platform_user_id']}"
    print(f"Account: {tokens.get('username')} | video {str(media['id'])[:8]} ({round(media.get('duration_s') or 0)} s)")

    async with httpx.AsyncClient(timeout=120.0) as client:
        src = await client.get(media["url"])
        src.raise_for_status()
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp) / "v.mp4"
            v.write_bytes(src.content)
            frame = Path(tmp) / "f.jpg"
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-v", "error", "-ss", "1", "-i", str(v), "-frames:v", "1", "-vf", "scale=1080:-2", str(frame)], check=True)
            cover = frame.read_bytes()
        print(f"Cover {len(cover) // 1024} KB")
        try:
            urn = await posts_api.upload_video(client, tokens["access_token"], owner, media["url"], cover)
        except httpx.HTTPStatusError as exc:
            print("Upload step refused:", exc.response.status_code, exc.response.text[:300])
            return 2
        print("Uploaded and finalized:", urn)
        began = time.monotonic()
        info = {}
        while time.monotonic() - began < 180:
            r = await client.get(f"{posts_api.LINKEDIN_REST}/videos/{urllib.parse.quote(urn, safe='')}", headers=posts_api._headers(tokens["access_token"]))
            info = r.json() if r.is_success else {"status": f"HTTP {r.status_code}"}
            if info.get("status") in ("AVAILABLE", "PROCESSING_FAILED") or str(info.get("status", "")).startswith("HTTP"):
                break
            await asyncio.sleep(5)
        print("Status:", info.get("status"), "| thumbnail kept:", bool(info.get("thumbnail")), "| waited", int(time.monotonic() - began), "s")
        print("No post was created.")
    return 0 if info.get("status") == "AVAILABLE" else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
