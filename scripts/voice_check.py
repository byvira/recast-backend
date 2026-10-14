"""A one-off, deliberate check of the standard voice: each language's sentence is spoken, transcribed back, and compared with the script.

    python scripts/voice_check.py [--out folder] [--language Spanish ...]

It measures clarity (how much of the script is heard back, in order), not naturalness: that needs a person. Audio files are saved so
anyone can listen. It uses a few short sentences, so it spends a few cents of free voice credit and about two minutes of the free
transcription allowance.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SENTENCES = {
    "English": ("en", "Most weekly plans fall apart by Wednesday. We plan for forty hours of work, but we only have twenty five real hours."),
    "Spanish": ("es", "La mayoría de los planes semanales se rompen el miércoles. Planeamos cuarenta horas de trabajo, pero solo tenemos veinticinco horas reales."),
    "French": ("fr", "La plupart des plannings de la semaine s'effondrent le mercredi. On prévoit quarante heures de travail, mais on n'a que vingt-cinq heures réelles."),
    "German": ("de", "Die meisten Wochenpläne brechen am Mittwoch zusammen. Wir planen vierzig Stunden Arbeit, haben aber nur fünfundzwanzig echte Stunden."),
    "Italian": ("it", "La maggior parte dei piani settimanali crolla il mercoledì. Pianifichiamo quaranta ore di lavoro, ma abbiamo solo venticinque ore reali."),
    "Dutch": ("nl", "De meeste weekplanningen vallen woensdag uit elkaar. We plannen veertig uur werk, maar hebben maar vijfentwintig echte uren."),
    "Japanese": ("ja", "ほとんどの週間計画は水曜日までに崩れます。四十時間の仕事を計画しても、実際の時間は二十五時間しかありません。"),
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "voice_check_audio")
    ap.add_argument("--language", action="append")
    ap.add_argument("--report", type=Path, help="also write the result here as JSON")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    from app.models.voice_settings import MemberVoiceSettings
    from app.pipelines.media import duration as audio_duration
    from app.pipelines.media.tts_generation import narration_blocker, synthesize_speech
    from app.video import transcribe
    from app.video.narrate.dub import roundtrip_similarity

    settings_obj = MemberVoiceSettings(id="check", workspace_id="voice-check", user_id="voice-check")
    rows = []
    for name, (code, text) in SENTENCES.items():
        if args.language and name not in args.language:
            continue
        blocker = narration_blocker(name, settings_obj)
        if blocker:
            rows.append({"language": name, "status": "blocked", "note": blocker})
            continue
        audio = await synthesize_speech(text=text, voice_settings=settings_obj, lexicon=None, workspace_id="voice-check", user_id="voice-check", language=code)
        if not audio:
            rows.append({"language": name, "status": "no_audio"})
            continue
        path = args.out / f"{code}.audio"
        path.write_bytes(audio)
        seconds = audio_duration.audio_duration_seconds(audio)
        words, _, _ = await transcribe._transcribe_piece(audio, 0, code, False)
        heard = " ".join(w["w"] for w in words)
        # Japanese has no spaces: compare by characters
        a, b = (list(text.replace(" ", "")), list(heard.replace(" ", ""))) if code == "ja" else (text, heard)
        score = roundtrip_similarity(" ".join(a) if code == "ja" else a, " ".join(b) if code == "ja" else b)
        rows.append({"language": name, "status": "ok", "seconds": round(seconds or 0, 1), "similarity": score, "heard": heard[:160], "file": str(path)})
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
