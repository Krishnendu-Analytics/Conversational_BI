from app.models import User
from app.security import OTPManager


class AuthService:

    @staticmethod
    def refresh_user_otp(user: User) -> str:
        otp = OTPManager.issue_code()

        # Store only the HASH in database
        user.otp_hash = OTPManager.hash_code(otp)

        user.otp_expires_at = OTPManager.expiry_time()

        # Return plaintext OTP only for email
        return otp