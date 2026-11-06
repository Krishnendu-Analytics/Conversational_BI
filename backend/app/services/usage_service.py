"""Read side of LLM usage (llm_usage): time series and totals for the AI
usage page. Token counts only — cost is deliberately not exposed here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LLMUsage, User

RANGES: dict[str, int] = {
    "1h": 3600,
    "3h": 3 * 3600,
    "12h": 12 * 3600,
    "1d": 86400,
    "1w": 7 * 86400,
    "30d": 30 * 86400,
}
# Candidate point spacings; the smallest giving <= MAX_POINTS points wins.
_STEPS = (300, 900, 3600, 6 * 3600, 86400)
MAX_POINTS = 72
MAX_CUSTOM_RANGE = timedelta(days=92)

FEATURE_LABELS: dict[str, str] = {
    "chat": "Chat",
    "chat_memory": "Chat memory",
    "schema_discovery": "Schema discovery",
    "semantic_model": "Semantic model",
    "dashboards": "Dashboards",
    "documents": "Documents",
    "datasets": "File datasets",
    "search": "Search",
    "other": "Other",
}


def feature_label(feature: str) -> str:
    return FEATURE_LABELS.get(feature) or feature.replace("_", " ").capitalize()


@dataclass(frozen=True)
class UsageWindow:
    start: datetime
    end: datetime
    step: int

    @property
    def points(self) -> int:
        span = (self.end - self.start).total_seconds()
        return max(1, -(-int(span) // self.step))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _floor(moment: datetime, step: int) -> datetime:
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % step, tz=timezone.utc)


def resolve_window(
    range_key: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    now: datetime | None = None,
) -> UsageWindow:
    """Preset (1h … 30d, ending now) or custom [start, end); raises ValueError."""
    if range_key == "custom":
        if start is None or end is None:
            raise ValueError("A custom range needs both start and end")
        start, end = _utc(start), _utc(end)
        if end <= start:
            raise ValueError("The range must end after it starts")
        if end - start > MAX_CUSTOM_RANGE:
            raise ValueError("A custom range can span at most 92 days")
    elif range_key in RANGES:
        end = _utc(now or datetime.now(timezone.utc))
        start = end - timedelta(seconds=RANGES[range_key])
    else:
        raise ValueError(f"Unknown range '{range_key}'")

    span = (end - start).total_seconds()
    step = next((s for s in _STEPS if span / s <= MAX_POINTS), _STEPS[-1])
    return UsageWindow(start=_floor(start, step), end=end, step=step)


def _empty_totals() -> dict[str, int]:
    return {"tokens": 0, "input_tokens": 0, "output_tokens": 0, "requests": 0}


def usage_report(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    window: UsageWindow,
) -> dict[str, Any]:
    """Series + totals for one tenant, or one user in it when ``user_id`` is set."""
    scope = [LLMUsage.tenant_id == tenant_id]
    if user_id is not None:
        scope.append(LLMUsage.user_id == user_id)
    in_window = [*scope, LLMUsage.bucket_start >= window.start, LLMUsage.bucket_start < window.end]

    rows = db.execute(
        select(
            LLMUsage.bucket_start,
            LLMUsage.feature,
            func.sum(LLMUsage.prompt_tokens),
            func.sum(LLMUsage.completion_tokens),
            func.sum(LLMUsage.calls),
        )
        .where(*in_window)
        .group_by(LLMUsage.bucket_start, LLMUsage.feature)
    ).all()

    n = window.points
    start_epoch = int(window.start.timestamp())
    series = [_empty_totals() for _ in range(n)]
    per_feature: dict[str, list[dict[str, int]]] = {}
    totals = _empty_totals()

    for bucket, feature, prompt, completion, calls in rows:
        idx = (int(_utc(bucket).timestamp()) - start_epoch) // window.step
        if not 0 <= idx < n:
            continue
        prompt, completion, calls = int(prompt or 0), int(completion or 0), int(calls or 0)
        for target in (series[idx], totals):
            target["input_tokens"] += prompt
            target["output_tokens"] += completion
            target["tokens"] += prompt + completion
            target["requests"] += calls
        feat = per_feature.setdefault(feature, [_empty_totals() for _ in range(n)])[idx]
        feat["tokens"] += prompt + completion
        feat["requests"] += calls

    times = [
        (window.start + timedelta(seconds=i * window.step)).isoformat() for i in range(n)
    ]
    features = [
        {
            "feature": feature,
            "label": feature_label(feature),
            "tokens": sum(p["tokens"] for p in points),
            "requests": sum(p["requests"] for p in points),
            "series": [{"t": t, **p} for t, p in zip(times, points)],
        }
        for feature, points in per_feature.items()
    ]
    features.sort(key=lambda f: f["tokens"], reverse=True)

    all_prompt, all_completion, all_calls = db.execute(
        select(
            func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsage.calls), 0),
        ).where(*scope)
    ).one()

    report: dict[str, Any] = {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "bucket_seconds": window.step,
        "totals": totals,
        "all_time": {
            "tokens": int(all_prompt) + int(all_completion),
            "input_tokens": int(all_prompt),
            "output_tokens": int(all_completion),
            "requests": int(all_calls),
        },
        "series": [{"t": t, **p} for t, p in zip(times, series)],
        "features": features,
        "users": None,
    }

    if user_id is None:
        user_rows = db.execute(
            select(
                User.id,
                User.email,
                func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens),
                func.sum(LLMUsage.calls),
            )
            .join(User, User.id == LLMUsage.user_id)
            .where(*in_window)
            .group_by(User.id, User.email)
            .order_by(func.sum(LLMUsage.prompt_tokens + LLMUsage.completion_tokens).desc())
            .limit(50)
        ).all()
        report["users"] = [
            {"user_id": str(uid), "email": email, "tokens": int(tokens or 0), "requests": int(calls or 0)}
            for uid, email, tokens, calls in user_rows
        ]
    return report
