"""Create or promote a platform super admin.

    python -m app.scripts.create_super_admin --email ops@example.com --password '…'
    python -m app.scripts.create_super_admin --email existing@example.com   # promote

A brand-new super admin gets their own organization (--org, default
"Platform") as its admin, so they can also use the app normally.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Tenant, User
from app.security import PasswordManager, validate_password_complexity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="required when the user doesn't exist yet")
    parser.add_argument("--org", default="Platform", help="organization for a new user")
    args = parser.parse_args(argv)

    init_db()
    email = args.email.strip().lower()
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            user.is_super_admin = True
            user.deleted_at = None
            if args.password:
                user.password_hash = PasswordManager.hash(validate_password_complexity(args.password))
                user.is_verified = True
            db.commit()
            print(f"Promoted {email} to super admin.")
            return 0

        if not args.password:
            print("User not found; pass --password to create it.", file=sys.stderr)
            return 1
        tenant = Tenant(name=args.org)
        db.add(tenant)
        db.flush()
        db.add(
            User(
                tenant_id=tenant.id,
                email=email,
                password_hash=PasswordManager.hash(validate_password_complexity(args.password)),
                is_verified=True,
                role="admin",
                is_super_admin=True,
            )
        )
        db.commit()
        print(f"Created super admin {email} in organization '{args.org}'.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
