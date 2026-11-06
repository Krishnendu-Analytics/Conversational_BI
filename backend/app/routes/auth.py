"""
Auth routes (signup, OTP, login, logout).

"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.email_service import send_email
from app.models import Department, DepartmentMember, RefreshToken, Tenant, User
from app.schemas import (
    AcceptInviteRequest,
    AuthConfigResponse,
    ChangePasswordRequest,
    DepartmentRef,
    InvitePreviewResponse,
    LoginRequest,
    MessageResponse,
    OTPVerificationRequest,
    RefreshRequest,
    ResendOTPRequest,
    SignupRequest,
    SignupResponse,
    TokenResponse,
    UpdateProfileRequest,
    UserMeResponse,
)
from app.authorization import resolve_user_from_access_token
from app.redis_client import AuthRateLimitUnavailableError, rate_limiter
from app.security import OTPManager, PasswordManager, TokenManager

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False)
settings = get_settings()

RESEND_GENERIC_MESSAGE = "If an account exists for this email, a new OTP has been sent."
OTP_VERIFY_GENERIC_ERROR = "Invalid or expired verification code"
LOGIN_GENERIC_ERROR = "Invalid email or password"


# ----- small helpers -----


def now_utc() -> datetime:
    """Current time in UTC."""
    return datetime.now(timezone.utc)


def make_timezone_aware(value: datetime) -> datetime:
    """If a datetime has no timezone, treat it as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def find_active_user_by_email(db: Session, email: str) -> User | None:
    """Find a user by email who is not soft-deleted."""
    query = select(User).where(User.email == email, User.deleted_at.is_(None))
    return db.scalar(query)


def find_user_by_email_any(db: Session, email: str) -> User | None:
    """Find a user by email, including soft-deleted ones (used in signup)."""
    return db.scalar(select(User).where(User.email == email))


def reset_otp_counters(user: User) -> None:
    """Clear OTP send limits so the user can receive a new code."""
    user.otp_send_count = 0
    user.otp_last_sent_at = None


def create_login_tokens(user: User, db: Session) -> TokenResponse:
    """
    Create access + refresh tokens after a successful login/verify.

    - access_token: short JWT (sent on every API call)
    - refresh_token: long random string (stored hashed in DB)
    """
    ensure_org_active(user, db)
    access_token = TokenManager.create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
    )

    refresh_token = TokenManager.issue_refresh_token()
    refresh_row = RefreshToken(
        user_id=user.id,
        token_hash=TokenManager.hash_refresh_token(refresh_token),
        expires_at=TokenManager.refresh_expiry_time(),
    )
    db.add(refresh_row)
    db.commit()

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        tenant_id=user.tenant_id,
        user_id=user.id,
        email=user.email,
    )


def ensure_org_active(user: User, db: Session) -> None:
    """Members of a disabled organization can't sign in or refresh."""
    tenant = db.get(Tenant, user.tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your organization has been disabled. Contact your administrator.",
        )


def build_me_response(user: User, db: Session) -> UserMeResponse:
    tenant = db.get(Tenant, user.tenant_id)
    departments = db.execute(
        select(Department.id, Department.name)
        .join(DepartmentMember, DepartmentMember.department_id == Department.id)
        .where(DepartmentMember.user_id == user.id)
        .order_by(Department.name)
    ).all()
    return UserMeResponse(
        id=user.id,
        email=user.email,
        tenant_id=user.tenant_id,
        tenant_name=tenant.name if tenant is not None else "",
        is_verified=user.is_verified,
        role=user.role,
        is_super_admin=bool(user.is_super_admin),
        departments=[DepartmentRef(id=d_id, name=name) for d_id, name in departments],
    )


def check_otp_send_limit(user: User) -> None:
    """Stop users from requesting OTP too often."""
    now = now_utc()

    if user.otp_last_sent_at is not None:
        last_sent = make_timezone_aware(user.otp_last_sent_at)
        seconds_since_last = (now - last_sent).total_seconds()

        # Too soon since the last OTP
        if seconds_since_last < settings.otp_resend_cooldown_seconds:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait before requesting another OTP",
            )

        # After one hour, reset the hourly counter
        if seconds_since_last >= 3600:
            user.otp_send_count = 0

    if user.otp_send_count >= settings.otp_resend_max_per_hour:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="OTP resend limit reached. Try again later.",
        )


def check_otp_verify_lockout(user: User) -> None:
    """Reject OTP issuance while verify lockout is active."""
    if user.otp_locked_until is None:
        return
    locked_until = make_timezone_aware(user.otp_locked_until)
    if now_utc() < locked_until:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=OTP_VERIFY_GENERIC_ERROR,
        )
    user.otp_locked_until = None


def create_and_send_otp(user: User, db: Session, commit: bool = True) -> None:
    """
    1) Check rate limit
    2) Create OTP
    3) Save hash in DB
    4) Email the plain OTP
    """
    check_otp_send_limit(user)
    check_otp_verify_lockout(user)

    # New OTP challenge resets attempts only when not locked (lockout already cleared).
    user.otp_verify_attempts = 0
    user.otp_locked_until = None

    otp_code = OTPManager.issue_code()
    user.otp_hash = OTPManager.hash_code(otp_code)
    user.otp_expires_at = OTPManager.expiry_time()
    user.otp_last_sent_at = now_utc()
    user.otp_send_count = (user.otp_send_count or 0) + 1
    db.flush()

    try:
        send_email(to_email=user.email, otp=otp_code)
    except HTTPException:
        db.rollback()
        raise

    if commit:
        db.commit()


def clear_otp_after_success(user: User) -> None:
    """OTP worked — mark verified and clear OTP fields."""
    user.is_verified = True
    user.otp_hash = None
    user.otp_expires_at = None
    user.otp_send_count = 0
    user.otp_verify_attempts = 0
    user.otp_locked_until = None


def signup_existing_unverified(
    user: User,
    body: SignupRequest,
    db: Session,
) -> SignupResponse:
    """User signed up before but never verified — update and resend OTP."""
    user.password_hash = PasswordManager.hash(body.password)

    tenant = db.get(Tenant, user.tenant_id)
    if tenant is not None and tenant.deleted_at is None:
        tenant.name = body.tenant_name

    reset_otp_counters(user)
    create_and_send_otp(user, db)

    return SignupResponse(
        message="Signup ok. OTP has been sent to your email.",
        email=user.email,
    )


def signup_restore_deleted(
    user: User,
    body: SignupRequest,
    db: Session,
) -> SignupResponse:
    """Soft-deleted account — bring it back and send OTP."""
    user.deleted_at = None
    user.is_verified = False
    user.password_hash = PasswordManager.hash(body.password)

    tenant = db.get(Tenant, user.tenant_id)
    if tenant is not None:
        tenant.deleted_at = None
        tenant.name = body.tenant_name

    reset_otp_counters(user)
    create_and_send_otp(user, db)

    return SignupResponse(
        message="Signup ok. OTP has been sent to your email.",
        email=user.email,
    )


def signup_brand_new(body: SignupRequest, email: str, db: Session) -> SignupResponse:
    """No account yet — create tenant + user, then send OTP."""
    tenant = Tenant(name=body.tenant_name)
    db.add(tenant)
    db.flush()

    user = User(
        tenant_id=tenant.id,
        email=email,
        password_hash=PasswordManager.hash(body.password),
        is_verified=False,
        role="admin",
    )
    db.add(user)
    db.flush()
    create_and_send_otp(user, db)

    return SignupResponse(
        message="If an account requires verification, a new OTP has been sent to your email.",
        email=user.email,
    )


# ----- FastAPI dependency: who is calling? -----


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Read the Bearer JWT and load that user from the DB."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    user = resolve_user_from_access_token(
        credentials.credentials,
        db,
        require_verified=False,
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified",
        )

    return user


def require_role(*allowed: str):
    """FastAPI dependency: current user must have one of the allowed roles."""

    def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user

    return _check


# ----- API endpoints -----


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, db: Session = Depends(get_db)) -> SignupResponse:
    """Create (or refresh) an account and email an OTP."""
    if not settings.allow_public_signup:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sign-up is by invitation only. Ask your organization admin for an invite.",
        )
    email = body.email.strip().lower()
    existing = find_user_by_email_any(db, email)

    # Already verified → cannot sign up again
    if existing is not None and existing.deleted_at is None and existing.is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Exists, not deleted, not verified → update password and resend OTP
    if existing is not None and existing.deleted_at is None and not existing.is_verified:
        return signup_existing_unverified(existing, body, db)

    # Soft-deleted → restore
    if existing is not None and existing.deleted_at is not None:
        return signup_restore_deleted(existing, body, db)

    # Brand new email
    return signup_brand_new(body, email, db)


@router.get("/config", response_model=AuthConfigResponse)
def auth_config() -> AuthConfigResponse:
    """Public: which sign-in options the login screens should offer."""
    return AuthConfigResponse(allow_public_signup=settings.allow_public_signup)


@router.get("/invites/{token}", response_model=InvitePreviewResponse)
def preview_invite(token: str, db: Session = Depends(get_db)) -> InvitePreviewResponse:
    """Public: who an invite link is for, so the accept page can greet them."""
    from app.services.org_service import find_open_invite

    invite = find_open_invite(db, token)
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This invite link is invalid or has expired",
        )
    user = db.get(User, invite.user_id)
    tenant = db.get(Tenant, invite.tenant_id)
    return InvitePreviewResponse(
        email=user.email if user else "",
        tenant_name=tenant.name if tenant else "",
    )


@router.post("/accept-invite", response_model=TokenResponse)
def accept_invite(body: AcceptInviteRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Public: set a password from an invite link, then sign in."""
    from app.services.org_service import accept_invite as accept

    user = accept(db, token=body.token, password=body.password)
    return create_login_tokens(user, db)


@router.post("/resend-otp", response_model=MessageResponse)
def resend_otp(body: ResendOTPRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """
    Resend OTP after signup (verify page).
    Keeps a generic message so outsiders cannot probe emails.
    """
    email = body.email.strip().lower()
    user = find_active_user_by_email(db, email)

    if user is not None:
        create_and_send_otp(user, db)

    return MessageResponse(message=RESEND_GENERIC_MESSAGE)


@router.post("/request-login-otp", response_model=MessageResponse)
def request_login_otp(body: ResendOTPRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """
    Send OTP for login.
    If the email is not registered, return a clear error (no email sent).
    """
    email = body.email.strip().lower()
    user = find_active_user_by_email(db, email)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please register.",
        )

    create_and_send_otp(user, db)
    return MessageResponse(message="OTP has been sent to your email.")


@router.post("/verify-otp", response_model=TokenResponse)
def verify_otp(body: OTPVerificationRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Check OTP, mark email verified, return login tokens."""
    email = body.email.strip().lower()
    user = find_active_user_by_email(db, email)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.otp_locked_until is not None:
        locked_until = make_timezone_aware(user.otp_locked_until)
        if now_utc() < locked_until:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=OTP_VERIFY_GENERIC_ERROR,
            )
        user.otp_locked_until = None

    if OTPManager.is_expired(user.otp_expires_at):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=OTP_VERIFY_GENERIC_ERROR,
        )

    otp_ok = OTPManager.verify(user.otp_hash, body.otp_code)
    if not otp_ok:
        user.otp_verify_attempts = int(user.otp_verify_attempts or 0) + 1
        if user.otp_verify_attempts >= settings.otp_verify_max_attempts:
            user.otp_hash = None
            user.otp_expires_at = None
            user.otp_verify_attempts = 0
            user.otp_locked_until = now_utc() + timedelta(
                minutes=settings.otp_lockout_minutes
            )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=OTP_VERIFY_GENERIC_ERROR,
        )

    clear_otp_after_success(user)
    db.flush()
    return create_login_tokens(user, db)


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Password login. Email must already be verified."""
    email = body.email.strip().lower()
    user = find_active_user_by_email(db, email)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=LOGIN_GENERIC_ERROR,
        )

    try:
        rate_limiter.ensure_distributed_backend()
    except AuthRateLimitUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=LOGIN_GENERIC_ERROR,
        ) from None

    login_key = f"login_fail:{email}"
    try:
        if rate_limiter.is_over_limit(login_key, settings.login_max_failures):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=LOGIN_GENERIC_ERROR,
            )
    except AuthRateLimitUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=LOGIN_GENERIC_ERROR,
        ) from None

    password_ok = PasswordManager.verify(body.password, user.password_hash)
    if not password_ok:
        try:
            count, exceeded = rate_limiter.record_failure(
                login_key,
                window_seconds=settings.login_lockout_minutes * 60,
                max_failures=settings.login_max_failures,
            )
        except AuthRateLimitUnavailableError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=LOGIN_GENERIC_ERROR,
            ) from None
        if exceeded:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=LOGIN_GENERIC_ERROR,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=LOGIN_GENERIC_ERROR,
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify OTP first",
        )

    return create_login_tokens(user, db)


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Issue a new access JWT. The refresh token is reused until it expires or logout."""
    token_hash = TokenManager.hash_refresh_token(body.refresh_token)
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    if stored.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    expires = make_timezone_aware(stored.expires_at)
    if now_utc() > expires:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        )

    user = db.get(User, stored.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    ensure_org_active(user, db)
    access_token = TokenManager.create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
    )
    return TokenResponse(
        access_token=access_token,
        refresh_token=body.refresh_token,
        tenant_id=user.tenant_id,
        user_id=user.id,
        email=user.email,
    )


@router.get("/me", response_model=UserMeResponse)
def me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMeResponse:
    """Return the logged-in user (needs Bearer token)."""
    return build_me_response(current_user, db)


@router.patch("/me", response_model=UserMeResponse)
def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMeResponse:
    """Update workspace name and/or account email."""
    if body.email is not None:
        next_email = body.email.strip().lower()
        if next_email != current_user.email:
            clash = find_active_user_by_email(db, next_email)
            if clash is not None and clash.id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered",
                )
            current_user.email = next_email

    tenant = db.get(Tenant, current_user.tenant_id)
    if body.tenant_name is not None and tenant is not None and body.tenant_name.strip() != tenant.name:
        if current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an organization admin can rename the organization",
            )
    if body.tenant_name is not None:
        if tenant is None or tenant.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workspace not found",
            )
        tenant.name = body.tenant_name.strip()

    db.commit()
    db.refresh(current_user)
    if tenant is not None:
        db.refresh(tenant)

    return build_me_response(current_user, db)


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Change password after verifying the current one. Keeps the session active."""
    if not PasswordManager.verify(body.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if body.current_password == body.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from the current password",
        )

    current_user.password_hash = PasswordManager.hash(body.new_password)
    db.commit()
    return MessageResponse(message="Password updated successfully.")


@router.post("/logout", response_model=MessageResponse)
def logout(
    body: RefreshRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Revoke this refresh token and all other active ones for the user."""
    token_hash = TokenManager.hash_refresh_token(body.refresh_token)
    stored = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    if stored.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    if stored.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    now = now_utc()
    active_tokens = db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == current_user.id,
            RefreshToken.revoked_at.is_(None),
        )
    ).all()

    for token in active_tokens:
        token.revoked_at = now

    db.commit()
    return MessageResponse(message="Logged out successfully.")
