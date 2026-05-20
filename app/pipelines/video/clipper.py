"""Video clipper — extracts highlight segments from a video."""


async def extract_highlights(video_file: bytes) -> list[bytes]:
    """Identify and extract highlight clips from *video_file*.

    Scores each scene by engagement potential (motion, faces, speech energy)
    and returns the top segments as individual clip byte strings.

    Args:
        video_file: Raw video bytes to clip.

    Returns:
        List of raw video bytes, one per extracted highlight clip.
    """
    # Placeholder: run scene scoring, trim with FFmpeg, return clip bytes
    return []
