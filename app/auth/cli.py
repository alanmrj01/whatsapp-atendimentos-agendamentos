"""Explicit local operator provisioning; never an HTTP signup endpoint."""
import argparse
import asyncio
import sys
from getpass import getpass
from uuid import UUID

from sqlalchemy import select

from app.auth.schemas import MembershipRole
from app.auth.security import hash_password, normalize_email
from app.core.database import dispose_engine, get_session_factory
from app.models import Business, BusinessUserMembership, User


async def create_user(email: str, password: str, super_admin: bool, business_id: UUID | None, role: str) -> None:
    try:
        async with get_session_factory()() as db, db.begin():
            if await db.scalar(select(User.id).where(User.email == email)):
                raise ValueError("User already exists; no changes made")
            if business_id and not await db.scalar(select(Business.id).where(Business.id == business_id, Business.active.is_(True))):
                raise ValueError("Active business not found")
            user = User(email=email, password_hash=hash_password(password),
                        platform_role="super_admin" if super_admin else None)
            db.add(user)
            await db.flush()
            if business_id:
                db.add(BusinessUserMembership(user_id=user.id, business_id=business_id, role=role))
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an authorized SaaS user (no secret arguments).")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--super-admin", action="store_true")
    scope.add_argument("--business-id", type=UUID)
    parser.add_argument("--role", choices=[role.value for role in MembershipRole], default="owner")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit("Interactive terminal required for secure password entry")
    try:
        email = normalize_email(input("Email: "))
        password = getpass("Password (12+ characters): ")
        if password != getpass("Confirm password: "):
            raise ValueError("Passwords do not match")
        asyncio.run(create_user(email, password, args.super_admin, args.business_id, args.role))
    except (ValueError, KeyboardInterrupt, EOFError):
        raise SystemExit("Provisioning rejected; verify input and existing account") from None
    except Exception:
        raise SystemExit("Provisioning failed; verify database configuration and schema") from None
    print("User created")


if __name__ == "__main__":
    main()
