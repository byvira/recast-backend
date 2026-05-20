"""Shared file storage helpers."""


async def upload_file(file: bytes, path: str) -> str:
    """Upload *file* bytes to the given storage *path*.

    Args:
        file: Raw bytes of the file to upload.
        path: Destination path within the storage bucket.

    Returns:
        Public or signed URL pointing to the uploaded file.
    """
    # Placeholder: upload to S3/GCS/R2 and return the resulting URL
    return f"https://storage.example.com/{path}"


async def get_file_url(path: str) -> str:
    """Return a (possibly signed) URL for the file at *path*.

    Args:
        path: Storage path of the file.

    Returns:
        Accessible URL for the file.
    """
    # Placeholder: generate a pre-signed URL or return a CDN URL
    return f"https://storage.example.com/{path}"
