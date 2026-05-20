"""Video analyzer — extracts metadata and scene information."""


async def analyze_video(video_file: bytes) -> dict:
    """Analyse *video_file* and return structured metadata.

    Extracts duration, frame rate, scenes, detected objects, and transcript
    by combining FFprobe metadata extraction with vision model analysis.

    Args:
        video_file: Raw video bytes to analyse.

    Returns:
        Dict with duration, fps, scenes, objects, and transcript keys.
    """
    # Placeholder: run FFprobe + vision model analysis on video bytes
    return {"duration": 0, "fps": 0, "scenes": [], "objects": [], "transcript": ""}
