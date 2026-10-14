"""The five calls the video render worker makes (contract version 1).

The worker is a separate program on its own machine. Its one credential is the worker token, which is accepted here and
nowhere else: it cannot read the database, other members' data or any provider key. With the switch off every call answers
503 and nothing is handed out.
"""
import hmac
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.middleware import limiter
from app.video import jobs

router = APIRouter()
logger = logging.getLogger(__name__)


async def worker_auth(
    authorization: str = Header(default=""),
    x_contract: str = Header(default=""),
) -> None:
    if not settings.VIDEO_WORKER_ENABLED:
        raise HTTPException(status_code=503, detail="The video worker is switched off.")
    presented = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    accepted = [t for t in (settings.VIDEO_WORKER_TOKEN, settings.VIDEO_WORKER_TOKEN_NEXT) if t]
    if not presented or not any(hmac.compare_digest(presented.encode(), t.encode()) for t in accepted):
        raise HTTPException(status_code=401, detail="Bad worker token.")
    if x_contract and x_contract != str(jobs.CONTRACT):
        raise HTTPException(status_code=400, detail=f"This backend speaks worker contract {jobs.CONTRACT}.")


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ClaimBody(_Body):
    worker_id: str = Field(default="", max_length=120)
    version: str = Field(default="", max_length=40)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class HeartbeatBody(_Body):
    lease_token: str = ""
    progress: dict[str, Any] = Field(default_factory=dict)


class UploadParamsBody(_Body):
    lease_token: str = ""
    purpose: str = "video"
    filename: str = Field(default="file", max_length=200)
    content_type: str = Field(default="", max_length=100)
    bytes: int = 0


class CompleteBody(_Body):
    lease_token: str = ""
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any] = Field(default_factory=dict)


class FailBody(_Body):
    lease_token: str = ""
    code: str = "internal"
    message: str = ""
    retryable: bool = False
    detail: Optional[Any] = None


def _lost() -> HTTPException:
    return HTTPException(status_code=409, detail="lease lost")


@router.post("/claim", dependencies=[Depends(worker_auth)])
@limiter.limit("600/minute")
async def claim(request: Request, body: ClaimBody, x_worker_id: str = Header(default=""), x_worker_version: str = Header(default="")) -> dict:
    job = await jobs.claim(body.worker_id or x_worker_id or "worker", body.version or x_worker_version, body.capabilities)
    return {"job": job}


@router.post("/jobs/{job_id}/heartbeat", dependencies=[Depends(worker_auth)])
@limiter.limit("1200/minute")
async def heartbeat(request: Request, job_id: str, body: HeartbeatBody) -> dict:
    try:
        return await jobs.heartbeat(job_id, body.lease_token, body.progress)
    except jobs.LeaseLost:
        raise _lost()


@router.post("/jobs/{job_id}/upload-params", dependencies=[Depends(worker_auth)])
@limiter.limit("600/minute")
async def upload_params(request: Request, job_id: str, body: UploadParamsBody) -> dict:
    try:
        return await jobs.upload_params(job_id, body.lease_token, body.purpose, body.filename, body.bytes)
    except jobs.LeaseLost:
        raise _lost()
    except jobs.JobError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.post("/jobs/{job_id}/complete", dependencies=[Depends(worker_auth)])
@limiter.limit("300/minute")
async def complete(request: Request, job_id: str, body: CompleteBody) -> dict:
    try:
        return await jobs.complete(job_id, body.lease_token, body.outputs, body.report)
    except jobs.LeaseLost:
        raise _lost()


@router.post("/jobs/{job_id}/fail", dependencies=[Depends(worker_auth)])
@limiter.limit("300/minute")
async def fail(request: Request, job_id: str, body: FailBody) -> dict:
    try:
        return await jobs.fail(job_id, body.lease_token, body.code, body.message, body.retryable, body.detail)
    except jobs.LeaseLost:
        raise _lost()
