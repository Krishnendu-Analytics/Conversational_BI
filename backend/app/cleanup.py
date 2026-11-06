"""Soft-delete old accounts that never verified their email."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserInvite

settings = get_settings()


def soft_delete_stale_unverified_users(db: Session) -> int:
    """
    Find unverified users older than N days and mark deleted_at.
    Returns how many users were updated.
    """
    days = settings.unverified_soft_delete_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    now = datetime.now(timezone.utc)

    # Invited users stay unverified until they accept; their invite expiry
    # (not this sweep) decides when the link stops working.
    invited = select(UserInvite.user_id).where(
        UserInvite.accepted_at.is_(None),
        UserInvite.revoked_at.is_(None),
    )
    users = db.scalars(
        select(User).where(
            User.is_verified.is_(False),
            User.deleted_at.is_(None),
            User.id.not_in(invited),
        )
    ).all()

    updated = 0
    for user in users:
        created = user.created_at
        if created is None:
            continue

        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        if created < cutoff:
            user.deleted_at = now
            updated = updated + 1

    if updated > 0:
        db.commit()

    return updated
