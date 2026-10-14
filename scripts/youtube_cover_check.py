"""A private, self-cleaning check that YouTube accepts a generated cover on a connected channel.

    python scripts/youtube_cover_check.py --workspace-name "Vira Studio's Workspace"

It makes a 6 second test video, uploads it as PRIVATE (the app's own upload code), sets a 1280x720 cover made by the cover feature's
crop, reads back what YouTube says, and deletes the video. Nothing is ever public. It prints the result and spends one upload
(about 1,600 of the day's 10,000 API units) and one thumbnail call (50 units). No token is printed.
"""
import argparse
import asyncio
import io
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-name", required=True)
    ap.add_argument("--keep", action="store_true", help="leave the private video in place instead of deleting it")
    args = ap.parse_args()

    import httpx
    from PIL import Image, ImageDraw

    from app.db.mongo import get_db
    from app.pipelines.publish import token_store
    from app.pipelines.publish.google.oauth import refresh_google_token
    from app.pipelines.publish.youtube.metadata import YouTubeMetadata
    from app.pipelines.publish.youtube.publisher import YOUTUBE_THUMBNAIL_URL, YouTubePublisher
    from app.video.covers import crop

    ws = await get_db()["workspaces"].find_one({"name": args.workspace_name})
    if not ws:
        print("No workspace with that name.")
        return 1
    workspace_id = ws.get("id") or str(ws["_id"])
    tokens = await token_store.get_token(workspace_id, "youtube")
    if not tokens:
        print("That workspace has no active YouTube connection.")
        return 1
    access = tokens["access_token"]
    print(f"Channel: {tokens.get('username')}")

    # a short test video and a cover frame cut from it by the same crop the cover feature uses
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "test.mp4"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi",
                        "-i", "sine=frequency=330:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
        frame = Path(tmp) / "frame.jpg"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-v", "error", "-ss", "2", "-i", str(video), "-frames:v", "1", str(frame)], check=True)
        cover_bytes, mode, _ = crop.fit_frame(frame.read_bytes(), 1280, 720, None)
        im = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
        ImageDraw.Draw(im).rectangle((0, 560, 1280, 720), fill=(15, 23, 42))
        ImageDraw.Draw(im).text((40, 610), "Ekavo cover check", fill=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        cover = buf.getvalue()
        data = video.read_bytes()
    print(f"Test video {len(data) // 1024} KB, cover {len(cover) // 1024} KB (limit 2048 KB), cut mode: {mode}")

    publisher = YouTubePublisher()
    meta = YouTubeMetadata(title="Ekavo cover check (private test, safe to delete)", description="A private test of automatic covers.",
                           tags=[], privacy_status="private")
    video_id = None
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in range(2):
            try:
                video_id = await publisher._upload_video(client, access, data, "video/mp4", meta, {})
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401 and attempt == 0 and tokens.get("refresh_token"):
                    print("Access token expired, renewing it ...")
                    access = (await refresh_google_token(tokens["refresh_token"]))["access_token"]
                    continue
                print("Upload refused:", exc.response.status_code, exc.response.text[:300])
                return 2
        print(f"Uploaded privately: video id {video_id}")
        resp = await client.post(YOUTUBE_THUMBNAIL_URL, params={"videoId": video_id, "uploadType": "media"},
                                 headers={"Authorization": f"Bearer {access}", "Content-Type": "image/jpeg"}, content=cover)
        print("Thumbnail:", resp.status_code, "ACCEPTED" if resp.is_success else resp.text[:300])
        info = await client.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet,status", "id": video_id},
                                headers={"Authorization": f"Bearer {access}"})
        if info.is_success and info.json().get("items"):
            item = info.json()["items"][0]
            print("Privacy:", item["status"].get("privacyStatus"), "| thumbnails now:", sorted((item["snippet"].get("thumbnails") or {}).keys()))
        if args.keep:
            print("Left in place (private):", video_id)
        else:
            d = await client.delete("https://www.googleapis.com/youtube/v3/videos", params={"id": video_id}, headers={"Authorization": f"Bearer {access}"})
            print("Deleted:", d.status_code in (200, 204), "" if d.status_code in (200, 204) else f"({d.status_code}: delete it in YouTube Studio, it is private)")
    return 0 if resp.is_success else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
