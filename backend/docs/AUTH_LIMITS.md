# Auth rate limiting

## OTP verify lockout

- Wrong OTP attempts are counted on `users.otp_verify_attempts`.
- After `otp_verify_max_attempts`, the account enters lockout until `otp_locked_until`.
- While lockout is active, `/resend-otp` and `/request-login-otp` reject issuance and do **not** reset counters.
- After lockout expires, a new OTP may be issued and attempts reset.
- Successful OTP verification clears attempts and lockout.

## Login failure rate limit

- Failed password logins increment a Redis key `login_fail:{email}` using atomic `INCR`.
- When the counter reaches `login_max_failures` within the lockout window, login returns HTTP 429.

## Production Redis requirement

Set `APP_ENV=production` (or `prod`) in production deployments.

Auth rate limiting **must** use Redis:

- Startup fails if Redis is unavailable in production.
- `/health` reports `auth_rate_limits_ok`, `auth_rate_limits_mode`, and `auth_rate_limits_redis`.
- Login returns HTTP 503 when production is configured without Redis (fail closed).

Development and tests may use the in-process fallback when `REDIS_URL` is empty.
