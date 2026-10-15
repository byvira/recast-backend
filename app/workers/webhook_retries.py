"""Sends the video webhook deliveries whose wait is over. Called every minute by the scheduler."""

from app.core.scheduler_lock import distributed_job_lock


@distributed_job_lock("retry_video_webhooks", ttl_seconds=55)
async def retry_video_webhooks() -> None:
    from app.video import webhooks

    await webhooks.retry_due()
