"""Scores the video creative prompts on the quality corpus and blocks a change that makes any score drop.

    python scripts/video_quality_regression.py --recorded answers.json          # score recorded model answers (free, repeatable)
    python scripts/video_quality_regression.py --live --limit 4 --record answers.json   # one deliberate live run, saved
    python scripts/video_quality_regression.py --recorded answers.json --check          # exit 1 when a score drops
    python scripts/video_quality_regression.py --recorded answers.json --write-baseline  # accept the current scores

A recorded answers file maps "<prompt>|<prompt text sha1>" to the model's answer, so a prompt change that alters the text is
scored by a new live run, never by a stale answer. Live runs spend free-tier quota, so they are explicit and limited.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.video.create import quality as Q  # noqa: E402


def _key(prompt: str, text: str) -> str:
    return f"{prompt}|{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recorded", type=Path)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--record", type=Path, help="with --live: save every answer here")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--baseline", type=Path, help="baseline file (default: tests/fixtures/video_quality/baseline.json)")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--language", action="append")
    ap.add_argument("--prompt", action="append")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    recorded: dict = json.loads(args.recorded.read_text(encoding="utf-8")) if args.recorded and args.recorded.exists() else {}
    saved: dict = dict(recorded)

    async def caller(prompt: str, text: str) -> dict:
        key = _key(prompt, text)
        if key in recorded:
            return recorded[key]
        if not args.live:
            return {}                                       # an answer that was never recorded counts as a failed answer
        from app.video.create.generate import slot_caller

        answer = await slot_caller(prompt)(text)
        saved[key] = answer
        return answer

    table = await Q.score(caller, languages=args.language, prompts=tuple(args.prompt) if args.prompt else None, limit=args.limit)
    print(json.dumps(table, indent=2, ensure_ascii=False))
    if args.live and args.record:
        args.record.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    baseline_path = args.baseline or Q.BASELINE_PATH
    weak = Q.below_pass_line(table)
    if weak:
        print("\nBelow the pass line (candidates for the stronger-model comparison):")
        for w in weak:
            print("  " + w)
    if args.write_baseline:
        baseline_path.write_text(json.dumps(table, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nBaseline written to {baseline_path}")
    if args.check:
        if not baseline_path.exists():
            print("There is no baseline yet. Run with --write-baseline first.")
            return 2
        bad = Q.drops(table, json.loads(baseline_path.read_text(encoding="utf-8")))
        if bad:
            print("\nBlocked: scores dropped:")
            for b in bad:
                print("  " + b)
            return 1
        print("\nNo score dropped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
