"""Server-Sent Events streaming helper."""

from collections.abc import AsyncGenerator

from fastapi.responses import StreamingResponse


async def stream_response(generator: AsyncGenerator[str, None]) -> StreamingResponse:
    """Wrap an async string generator in an SSE StreamingResponse.

    Args:
        generator: Async generator that yields ``data: ...\\n\\n`` formatted
                   SSE strings.

    Returns:
        StreamingResponse configured with ``text/event-stream`` media type.
    """
    return StreamingResponse(generator, media_type="text/event-stream")
