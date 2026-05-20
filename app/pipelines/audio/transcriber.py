"""Audio transcriber — converts audio files to text."""


async def transcribe(audio_file: bytes) -> str:
    """Transcribe *audio_file* bytes to plain text.

    Sends the audio to a speech-to-text service (e.g. Whisper or Deepgram)
    and returns the full transcript with punctuation.

    Args:
        audio_file: Raw audio bytes (WAV, MP3, or similar format).

    Returns:
        Transcribed text string.
    """
    # Placeholder: call Whisper API or Deepgram with audio_file bytes
    return ""
