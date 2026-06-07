"""User webhook CRUD and test routes."""
from __future__ import annotations

from typing import Any

import requests as _req
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api import auth
from app.api import user_webhooks as webhook_store
from app.db import session as db_session

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Per-user Discord webhook registrations
# ─────────────────────────────────────────────────────────────────────────────

class WebhookCreateBody(BaseModel):
    label: str = ""
    webhookUrl: str
    eventTypes: list[str] = webhook_store.DEFAULT_EVENT_TYPES
    platforms: list[str] = webhook_store.DEFAULT_PLATFORMS
    maxDepositManwon: int | None = None
    maxRentManwon: int | None = None
    useAreaFilter: bool = True
    # AND-composed with the polygon area filter inside the matcher: when
    # both are set, a listing must be tagged for ANY of these regions AND
    # fall inside the user's polygon. Empty list + useAreaFilter=False
    # disables the location restriction entirely.
    regionIds: list[int] = []


class WebhookUpdateBody(BaseModel):
    label: str | None = None
    webhookUrl: str | None = None
    isActive: bool | None = None
    eventTypes: list[str] | None = None
    platforms: list[str] | None = None
    maxDepositManwon: int | None = None
    maxRentManwon: int | None = None
    useAreaFilter: bool | None = None
    regionIds: list[int] | None = None


def _webhook_error_to_http(exc: webhook_store.WebhookError) -> HTTPException:
    code = {"unknown": 404, "forbidden": 404, "invalid": 400, "limit": 409}.get(
        exc.reason, 400
    )
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/api/user/webhooks")
async def list_user_webhooks(user: auth.User = Depends(auth.current_user)):
    return webhook_store.list_webhooks(user.id)


@router.post("/api/user/webhooks", status_code=201)
async def create_user_webhook(body: WebhookCreateBody,
                              user: auth.User = Depends(auth.current_user)):
    try:
        return webhook_store.create_webhook(
            user.id,
            label=body.label,
            webhook_url=body.webhookUrl,
            event_types=body.eventTypes,
            platforms=body.platforms,
            max_deposit_manwon=body.maxDepositManwon,
            max_rent_manwon=body.maxRentManwon,
            use_area_filter=body.useAreaFilter,
            region_ids=body.regionIds,
        )
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)


@router.patch("/api/user/webhooks/{webhook_id}")
async def update_user_webhook(webhook_id: int, body: WebhookUpdateBody,
                              user: auth.User = Depends(auth.current_user)):
    kwargs: dict[str, Any] = {}
    if body.label is not None:
        kwargs["label"] = body.label
    if body.webhookUrl is not None:
        kwargs["webhook_url"] = body.webhookUrl
    if body.isActive is not None:
        kwargs["is_active"] = body.isActive
    if body.eventTypes is not None:
        kwargs["event_types"] = body.eventTypes
    if body.platforms is not None:
        kwargs["platforms"] = body.platforms
    if body.maxDepositManwon is not None or "maxDepositManwon" in body.model_fields_set:
        kwargs["max_deposit_manwon"] = body.maxDepositManwon
    if body.maxRentManwon is not None or "maxRentManwon" in body.model_fields_set:
        kwargs["max_rent_manwon"] = body.maxRentManwon
    if body.useAreaFilter is not None:
        kwargs["use_area_filter"] = body.useAreaFilter
    if body.regionIds is not None:
        kwargs["region_ids"] = body.regionIds
    try:
        return webhook_store.update_webhook(webhook_id, user.id, **kwargs)
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)


@router.delete("/api/user/webhooks/{webhook_id}", status_code=204)
async def delete_user_webhook(webhook_id: int,
                              user: auth.User = Depends(auth.current_user)):
    try:
        webhook_store.delete_webhook(webhook_id, user.id)
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)


@router.post("/api/user/webhooks/{webhook_id}/test")
async def test_user_webhook(webhook_id: int,
                            user: auth.User = Depends(auth.current_user)):
    try:
        wh = webhook_store.get_webhook(webhook_id, user.id)
    except webhook_store.WebhookError as exc:
        raise _webhook_error_to_http(exc)
    label = wh["label"] or "내 알림"
    wh_type = webhook_store.detect_webhook_type(wh["webhookUrl"])
