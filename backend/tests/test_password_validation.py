import pytest
from pydantic import ValidationError

from app.schemas import SignupRequest, ChangePasswordRequest


def test_signup_accepts_strong_password() -> None:
    request = SignupRequest(
        tenant_name="Test Tenant",
        email="test@example.com",
        password="Password@123",
    )

    assert request.password == "Password@123"


def test_signup_rejects_short_password() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            tenant_name="Test Tenant",
            email="test@example.com",
            password="Pass@1",
        )


def test_signup_rejects_password_without_uppercase() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            tenant_name="Test Tenant",
            email="test@example.com",
            password="password@123",
        )


def test_signup_rejects_password_without_lowercase() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            tenant_name="Test Tenant",
            email="test@example.com",
            password="PASSWORD@123",
        )


def test_signup_rejects_password_without_number() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            tenant_name="Test Tenant",
            email="test@example.com",
            password="Password@abc",
        )


def test_signup_rejects_password_without_special_character() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            tenant_name="Test Tenant",
            email="test@example.com",
            password="Password12345",
        )


def test_signup_rejects_password_with_leading_or_trailing_space() -> None:
    with pytest.raises(ValidationError):
        SignupRequest(
            tenant_name="Test Tenant",
            email="test@example.com",
            password=" Password@123",
        )


def test_change_password_accepts_strong_password() -> None:
    request = ChangePasswordRequest(
        current_password="OldPassword@123",
        new_password="NewPassword@123",
    )

    assert request.new_password == "NewPassword@123"


def test_change_password_rejects_weak_password() -> None:
    with pytest.raises(ValidationError):
        ChangePasswordRequest(
            current_password="OldPassword@123",
            new_password="password",
        )
