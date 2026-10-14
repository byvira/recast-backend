"""Measures two real limits, with nothing left behind.

    python scripts/video_limits_check.py [--sizes-mb 25 50 100 200] [--minutes 0]

1. Worker readiness: is the worker online, which version, and which speech languages it reports (Japanese appears once v0.4.3 has
   installed the extra package).
2. Upload cap: makes test videos of growing size, uploads each to storage, reports the size that is accepted or the first one that is
   refused, then deletes every test file. One upload per size, so spend is small and bounded.

No token is printed. Test files are removed in a finally block.
"""
import argparse
import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_video(path: Path, target_mb: int) -> int:
    """A real H.264 file close to target_mb. Random-looking noise keeps the bitrate high so the length stays short."""
    seconds = max(10, int(target_mb * 8 / 6))  # about 6 Mbit/s
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:v", "libx264", "-b:v", "6M", "-maxrate", "6M",
         "-bufsize", "12M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path.stat().st_size


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes-mb", nargs="*", type=int, default=[25, 50, 100, 200])
    args = ap.parse_args()

    from app.shared.storage import ContentType, delete_file_by_url, upload_file
    from app.video import jobs

    status = await jobs.worker_status()
    print("Worker online:", status["online"], "| version:", status.get("version"), "| last seen:", status.get("last_seen"))
    caps = status.get("capabilities") or {}
    print("  speech:", caps.get("speech"), "| languages:", caps.get("speech_languages"), "| frames:", caps.get("frames"), "| faces:", caps.get("faces"))
    print("  jobs:", status["jobs"], "| waiting seconds:", status["oldest_waiting_seconds"])

    stored: list[str] = []
    last_ok = 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for mb in args.sizes_mb:
                path = Path(tmp) / f"t{mb}.mp4"
                actual = make_video(path, mb) // (1024 * 1024)
                began = time.monotonic()
                try:
                    url = await upload_file(path.read_bytes(), ContentType.VIDEO, "limits-check", f"ekavo-limits-{mb}.mp4")
                    stored.append(url)
                    last_ok = actual
                    print(f"Upload {actual} MB: accepted in {time.monotonic() - began:.0f} s")
                except Exception as exc:  # noqa: BLE001
                    print(f"Upload {actual} MB: REFUSED ({type(exc).__name__}: {str(exc)[:160]})")
                    break
                finally:
                    path.unlink(missing_ok=True)
        print(f"Largest accepted in this run: {last_ok} MB")
    finally:
        for url in stored:
            try:
                await delete_file_by_url(url)
            except Exception as exc:  # noqa: BLE001
                print("Could not remove a test file:", exc)
        print("Test files removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
