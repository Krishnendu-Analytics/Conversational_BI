"""AI usage page: token usage over time for the signed-in user or, for
admins, the whole organization. Token counts only — no cost."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routes.auth import get_current_user
from app.services.usage_service import resolve_window, usage_report

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


class UsageTotals(BaseModel):
    tokens: int
    input_tokens: int
    output_tokens: int
    requests: int


class UsagePoint(UsageTotals):
    t: datetime


class FeatureUsage(BaseModel):
    feature: str
    label: str
    tokens: int
    requests: int
    series: list[UsagePoint]


class UserUsage(BaseModel):
    user_id: str
    email: str
    tokens: int
    requests: int


class UsageReport(BaseModel):
    scope: Literal["me", "organization"]
    range: str
    start: datetime
    end: datetime
    bucket_seconds: int
    totals: UsageTotals
    all_time: UsageTotals
    series: list[UsagePoint]
    features: list[FeatureUsage]
    users: list[UserUsage] | None = None


@router.get("", response_model=UsageReport)
def get_usage(
    range: str = Query(default="1d", pattern="^(1h|3h|12h|1d|1w|30d|custom)$"),
    start: datetime | None = None,
    end: datetime | None = None,
    scope: Literal["me", "organization"] | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UsageReport:
    is_admin = current_user.role == "admin"
    scope = scope or ("organization" if is_admin else "me")
    if scope == "organization" and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can see organization usage",
        )
    try:
        window = resolve_window(range, start=start, end=end)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    report = usage_report(
        db,
        tenant_id=current_user.tenant_id,
        user_id=None if scope == "organization" else current_user.id,
        window=window,
    )
    return UsageReport(scope=scope, range=range, **report)
