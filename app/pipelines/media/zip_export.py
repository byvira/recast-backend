"""ZIP export for a multi-slide ImageAsset — every real rendered slide,
bundled via stdlib zipfile (no new dependency, per the plan's own naming
in PROGRESS.md's Deferred section)."""

import io
import zipfile


def build_slides_zip(slides: list[tuple[str, bytes]]) -> bytes:
    """slides: [(filename, real image bytes), ...], one per real rendered
    slide. Returns real zip bytes, deflate-compressed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, data in slides:
            zf.writestr(filename, data)
    return buf.getvalue()
