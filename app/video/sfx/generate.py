"""Makes the sound effects pack. Every effect is synthesised here with ffmpeg from plain tones and noise, so there is no recording, sample or
library behind it: the pack is Ekavo's own work, dedicated to the public domain (CC0 1.0), and each effect carries that licence record.

Run once when the pack changes:  python -m app.video.sfx.generate
It writes `files/<id>.mp3` and `catalog.json` (name, kind, length, size, checksum and licence of each effect)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
FILES = HERE / "files"
LICENCE = {
    "name": "CC0 1.0 Universal (public domain dedication)",
    "url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "author": "Ekavo",
    "origin": "Synthesised by app/video/sfx/generate.py from tones and noise. No recording or third-party sample is used.",
}

# id: (title, category, ffmpeg source and filters). Every one ends in an even loudness stage so effects sit at a similar level.
EFFECTS: dict[str, tuple[str, str, str]] = {
    "whoosh_soft": ("Soft whoosh", "whoosh", "anoisesrc=d=0.8:c=pink:r=44100:a=0.9,highpass=f=250,lowpass=f=3500,afade=t=in:st=0:d=0.35,afade=t=out:st=0.35:d=0.45"),
    "whoosh_fast": ("Fast whoosh", "whoosh", "anoisesrc=d=0.45:c=white:r=44100:a=0.9,highpass=f=600,lowpass=f=7000,afade=t=in:st=0:d=0.15,afade=t=out:st=0.15:d=0.3"),
    "whoosh_down": ("Falling whoosh", "whoosh", "aevalsrc='0.6*sin(2*PI*(1800*t-1500*t*t))*exp(-2.2*t)+0.15*(random(0)-0.5)*exp(-3*t)':d=0.7:s=44100"),
    "swish": ("Swish", "whoosh", "anoisesrc=d=0.3:c=white:r=44100:a=0.8,bandpass=f=3000:width_type=h:w=3000,afade=t=in:st=0:d=0.1,afade=t=out:st=0.1:d=0.2"),
    "pop": ("Pop", "hit", "aevalsrc='0.9*sin(2*PI*(900-3200*t)*t)*exp(-28*t)':d=0.16:s=44100"),
    "click": ("Click", "hit", "aevalsrc='0.8*(random(0)-0.5)*exp(-180*t)+0.3*sin(2*PI*2400*t)*exp(-120*t)':d=0.05:s=44100"),
    "tick": ("Tick", "hit", "aevalsrc='0.7*sin(2*PI*1800*t)*exp(-260*t)':d=0.04:s=44100"),
    "hit": ("Punch", "hit", "aevalsrc='0.95*sin(2*PI*(90-60*t)*t)*exp(-9*t)+0.35*(random(0)-0.5)*exp(-40*t)':d=0.45:s=44100"),
    "boom": ("Deep boom", "hit", "aevalsrc='0.95*sin(2*PI*(55-25*t)*t)*exp(-3.2*t)+0.25*(random(0)-0.5)*exp(-12*t)':d=1.1:s=44100"),
    "riser": ("Riser", "build", "aevalsrc='0.55*sin(2*PI*(260*t+420*t*t))*(t/1.6)+0.12*(random(0)-0.5)*(t/1.6)':d=1.6:s=44100,afade=t=out:st=1.5:d=0.1"),
    "ding": ("Ding", "tone", "aevalsrc='0.7*sin(2*PI*1318.5*t)*exp(-4.5*t)+0.25*sin(2*PI*2637*t)*exp(-7*t)':d=1.0:s=44100"),
    "sparkle": ("Sparkle", "tone", "aevalsrc='0.45*(sin(2*PI*2093*t)*exp(-22*t)+sin(2*PI*2637*(t-0.07))*exp(-22*(t-0.07))*gte(t,0.07)+sin(2*PI*3136*(t-0.14))*exp(-22*(t-0.14))*gte(t,0.14))':d=0.6:s=44100"),
}


def _run(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-600:])
    return out.stdout


def make(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> list[dict]:
    FILES.mkdir(parents=True, exist_ok=True)
    catalog = []
    for eid, (title, category, source) in EFFECTS.items():
        target = FILES / f"{eid}.mp3"
        # the source and filters, then an even level, a stereo copy, and the mp3
        graph = f"{source},loudnorm=I=-18:TP=-2:LRA=7,aformat=sample_rates=44100:channel_layouts=stereo"
        lavfi = graph.split(",", 1)
        if source.startswith("aevalsrc") or source.startswith("anoisesrc"):
            args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", graph, "-map_metadata", "-1", "-b:a", "128k", str(target)]
        else:  # pragma: no cover - every source above is a generator
            args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", lavfi[0], str(target)]
        _run(args)
        info = json.loads(_run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", str(target)]))["format"]
        data = target.read_bytes()
        catalog.append({
            "id": eid, "title": title, "category": category, "duration_s": round(float(info["duration"]), 2), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "licence": LICENCE,
        })
    (HERE / "catalog.json").write_text(json.dumps({"effects": catalog}, indent=2), encoding="utf-8")
    return catalog


if __name__ == "__main__":
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("ffmpeg and ffprobe are needed to make the pack.")
    for row in make():
        print(f"{row['id']:<12} {row['duration_s']:>5}s {row['bytes']:>7} bytes")
