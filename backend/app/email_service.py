"""Send OTP emails with SendGrid."""

import logging
from pathlib import Path

from fastapi import HTTPException, status
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from app.config import get_settings

logger = logging.getLogger(__name__)
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "email.html"


def render_otp_email(otp: str, minutes: int) -> str:
    """Load email.html and put the OTP into it."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("{{ otp }}", otp)
    html = html.replace("{{ minutes }}", str(minutes))
    return html


def send_email(to_email: str, otp: str) -> None:
    """Send one OTP email. Raises HTTPException if SendGrid fails."""
    settings = get_settings()

    if not settings.sendgrid_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service is not configured",
        )

    html_body = render_otp_email(otp, settings.otp_expire_minutes)
    message = Mail(
        from_email=settings.from_email,
        to_emails=to_email,
        subject="Email Verification OTP",
        html_content=html_body,
    )

    try:
        client = SendGridAPIClient(settings.sendgrid_api_key)
        client.send(message)
    except Exception:
        logger.exception("Failed to send OTP email to %s", to_email)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send OTP email",
        )


def _render_invite_email(org_name: str, invite_url: str, days: int) -> str:
    from html import escape

    org = escape(org_name)
    url = escape(invite_url, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en"><body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;color:#222;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f5f5;padding:32px 16px;"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:520px;background:#ffffff;border-radius:8px;padding:32px;"><tr><td>
<h2 style="margin:0 0 16px;font-size:22px;font-weight:700;">You're invited to {org}</h2>
<p style="margin:0 0 20px;font-size:15px;line-height:1.5;">Set a password to activate your account.</p>
<p style="margin:0 0 24px;"><a href="{url}" style="display:inline-block;background:#222;color:#fff;text-decoration:none;padding:12px 20px;border-radius:6px;font-size:15px;">Accept invite</a></p>
<p style="margin:0 0 12px;font-size:13px;line-height:1.5;color:#555;">This link expires in {days} days. If you weren't expecting it, ignore this email.</p>
</td></tr></table></td></tr></table></body></html>"""


def send_invite_email(to_email: str, *, org_name: str, invite_url: str) -> bool:
    """Send an invite link. Returns False (never raises) when email isn't
    configured or SendGrid fails, so the admin can share the link instead."""
    settings = get_settings()
    if not settings.sendgrid_api_key or not settings.from_email:
        return False
    message = Mail(
        from_email=settings.from_email,
        to_emails=to_email,
        subject=f"You're invited to {org_name}",
        html_content=_render_invite_email(org_name, invite_url, settings.invite_expire_days),
    )
    try:
        SendGridAPIClient(settings.sendgrid_api_key).send(message)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send invite email to %s", to_email)
        return False
    return True
