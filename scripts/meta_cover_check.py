"""Checks that Facebook and Instagram accept a generated cover, without anything becoming public.

    python scripts/meta_cover_check.py --workspace-name "Vira Studio's Workspace"

Facebook: the test video goes up as an UNPUBLISHED page video with the cover as its thumbnail, is read back, then deleted.
Instagram: a Reel container is created with the cover and polled until it is processed. The publish call is never made, so no post
exists; the container expires by itself within a day. The test video and cover are put in storage briefly and removed at the end.
No token is printed.
"""
import argparse
import asyncio
import io
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GRAPH = "https://graph.facebook.com/v21.0"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace-name", required=True)
    ap.add_argument("--only", choices=["facebook", "instagram"])
    args = ap.parse_args()

    import httpx
    from PIL import Image, ImageDraw

    from app.db.mongo import get_db
    from app.pipelines.publish import token_store
    from app.pipelines.publish.meta import facebook as fb_mod
    from app.shared.storage import ContentType, delete_file_by_url, upload_file
    from app.video.covers import crop

    global GRAPH
    GRAPH = getattr(fb_mod, "GRAPH_BASE", GRAPH)
    ws = await get_db()["workspaces"].find_one({"name": args.workspace_name})
    wid = ws.get("id") or str(ws["_id"])
    fb = await token_store.get_token(wid, "facebook")
    ig = await token_store.get_token(wid, "instagram")

    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "t.mp4"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=720x1280:r=30:d=6", "-f", "lavfi",
                        "-i", "sine=frequency=330:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)], check=True)
        frame = Path(tmp) / "f.jpg"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-v", "error", "-ss", "2", "-i", str(video), "-frames:v", "1", str(frame)], check=True)
        # an unmistakable cover (magenta), so a thumbnail taken from the video itself can be told apart from ours
        im = Image.new("RGB", (1080, 1920), (230, 20, 160))
        ImageDraw.Draw(im).rectangle((0, 1500, 1080, 1700), fill=(15, 23, 42))
        ImageDraw.Draw(im).text((60, 1580), "Ekavo cover check", fill=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        cover = buf.getvalue()
        data = video.read_bytes()

    video_url = await upload_file(data, ContentType.VIDEO, "cover-check", "ekavo-cover-check.mp4")
    cover_url = await upload_file(cover, ContentType.IMAGE, "cover-check", "ekavo-cover-check.jpg")
    print(f"Temporary files stored. Cover {len(cover) // 1024} KB (1080x1920).")
    code = 0
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            if fb and args.only in (None, "facebook"):
                r = await client.post(f"{GRAPH}/{fb['platform_user_id']}/videos",
                                      data={"file_url": video_url, "description": "Ekavo cover check (unpublished test)", "published": "false",
                                            "access_token": fb["access_token"]},
                                      files={"thumb": ("cover.jpg", cover, "image/jpeg")})
                print("Facebook:", r.status_code, "unpublished video created" if r.is_success else r.text[:350])
                if r.is_success:
                    vid = r.json().get("id")
                    # Facebook makes the thumbnails after it has processed the video, so wait for them (up to 4 minutes)
                    began = time.monotonic()
                    thumbs, j = [], {}
                    while time.monotonic() - began < 240:
                        info = await client.get(f"{GRAPH}/{vid}", params={"fields": "published,status,thumbnails{uri,is_preferred}", "access_token": fb["access_token"]})
                        j = info.json() if info.is_success else {}
                        thumbs = (j.get("thumbnails") or {}).get("data") or []
                        if thumbs:
                            break
                        await asyncio.sleep(10)
                    print("  video status:", (j.get("status") or {}).get("video_status"), "| waited", int(time.monotonic() - began), "s")
                    for t in ([x for x in thumbs if x.get("is_preferred")] or thumbs[:1]):
                        try:
                            img = Image.open(io.BytesIO((await client.get(t["uri"])).content)).convert("RGB").resize((1, 1))
                            r_, g_, b_ = img.getpixel((0, 0))
                            print(f"  preferred thumbnail average colour ({r_},{g_},{b_}) ->", "OUR COVER (magenta)" if r_ > 150 and g_ < 90 and b_ > 90 else "not our cover")
                        except Exception as exc:  # noqa: BLE001
                            print("  could not read a thumbnail:", exc)
                    print("  published:", j.get("published"), "| thumbnails:", len(thumbs), "| a preferred (custom) one:", any(t.get("is_preferred") for t in thumbs))
                    d = await client.delete(f"{GRAPH}/{vid}", params={"access_token": fb["access_token"]})
                    print("  deleted:", d.is_success)
                else:
                    code = 3
            if ig and args.only in (None, "instagram"):
                r = await client.post(f"{GRAPH}/{ig['platform_user_id']}/media",
                                      params={"media_type": "REELS", "video_url": video_url, "cover_url": cover_url,
                                              "caption": "Ekavo cover check (never published)", "access_token": ig["access_token"]})
                print("Instagram container:", r.status_code, "created" if r.is_success else r.text[:350])
                if r.is_success:
                    cid = r.json()["id"]
                    began = time.monotonic()
                    status = None
                    while time.monotonic() - began < 150:
                        s = await client.get(f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": ig["access_token"]})
                        status = s.json() if s.is_success else {"status_code": f"HTTP {s.status_code}"}
                        if status.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                            break
                        await asyncio.sleep(5)
                    print("  processing result:", status.get("status_code"), "|", str(status.get("status", ""))[:200], "(never published)")
                    if status.get("status_code") != "FINISHED":
                        code = 4
                else:
                    code = 4
    finally:
        for u in (video_url, cover_url):
            try:
                await delete_file_by_url(u)
            except Exception as exc:  # noqa: BLE001
                print("Could not remove a temporary file:", exc)
    print("Temporary files removed.")
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
