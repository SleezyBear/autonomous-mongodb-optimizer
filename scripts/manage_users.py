#!/usr/bin/env python3
"""Create, list, or update control-plane users (development and operations).

Examples::

    ./nosql/bin/python scripts/manage_users.py create --email ops@example.com --role OPERATOR
    ./nosql/bin/python scripts/manage_users.py create --email lead@example.com --role APPROVER
    ./nosql/bin/python scripts/manage_users.py list
    ./nosql/bin/python scripts/manage_users.py set-role --email ops@example.com --role ADMIN
    ./nosql/bin/python scripts/manage_users.py disable --email ops@example.com

Passwords are read interactively (or from ``CONTROL_PLANE_PASSWORD`` for
automation) and stored only as Argon2id hashes.  Uses ``DATABASE_URL`` or the
local development default.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.auth.security import PasswordService, Role  # noqa: E402
from app.runtime import runtime_settings  # noqa: E402


def _password_problem(password: str) -> str | None:
    try:
        PasswordService._validate_password(password)
    except ValueError as error:
        return str(error)
    return None


def _password() -> str:
    supplied = os.environ.get("CONTROL_PLANE_PASSWORD")
    if supplied:
        problem = _password_problem(supplied)
        if problem:
            raise SystemExit(f"CONTROL_PLANE_PASSWORD is not valid: {problem}")
        return supplied
    while True:
        first = getpass.getpass("Password (12-128 characters): ")
        problem = _password_problem(first)
        if problem:
            print(f"{problem} Please try again.", file=sys.stderr)
            continue
        if first != getpass.getpass("Repeat password: "):
            print("Passwords do not match. Please try again.", file=sys.stderr)
            continue
        return first


async def main(arguments: argparse.Namespace) -> int:
    engine = create_async_engine(runtime_settings().database_url)
    try:
        async with engine.begin() as connection:
            if arguments.command == "list":
                rows = (await connection.execute(text("SELECT email, role, status, created_at FROM users ORDER BY created_at"))).mappings().all()
                for row in rows:
                    print(f"{row['email']:<40} {row['role']:<9} {row['status']:<9} {row['created_at']:%Y-%m-%d}")
                return 0
            email = arguments.email.strip().lower()
            if arguments.command == "create":
                if await connection.scalar(text("SELECT 1 FROM users WHERE email = :email"), {"email": email}):
                    print(f"{email} already exists. To change its password: {sys.argv[0]} reset-password --email {email}", file=sys.stderr)
                    return 1
                password_hash = PasswordService().hash_password(_password())
                await connection.execute(
                    text("INSERT INTO users (id, created_at, updated_at, email, password_hash, role, status, failed_login_count) VALUES (:id, now(), now(), :email, :hash, :role, 'ACTIVE', 0)"),
                    {"id": uuid4(), "email": email, "hash": password_hash, "role": Role(arguments.role).value},
                )
                print(f"Created {Role(arguments.role).value} {email}")
            elif arguments.command == "set-role":
                result = await connection.execute(text("UPDATE users SET role = :role, updated_at = now() WHERE email = :email"), {"email": email, "role": Role(arguments.role).value})
                print("Updated" if result.rowcount else "No such user")
            elif arguments.command == "disable":
                result = await connection.execute(text("UPDATE users SET status = 'DISABLED', updated_at = now() WHERE email = :email"), {"email": email})
                print("Disabled" if result.rowcount else "No such user")
            elif arguments.command == "reset-password":
                result = await connection.execute(
                    text("UPDATE users SET password_hash = :hash, failed_login_count = 0, locked_until = NULL, updated_at = now() WHERE email = :email"),
                    {"email": email, "hash": PasswordService().hash_password(_password())},
                )
                print("Password reset" if result.rowcount else "No such user")
    finally:
        await engine.dispose()
    return 0


def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("create", "set-role"):
        sub = commands.add_parser(name)
        sub.add_argument("--email", required=True)
        sub.add_argument("--role", required=True, choices=[role.value for role in Role])
    for name in ("disable", "reset-password"):
        commands.add_parser(name).add_argument("--email", required=True)
    try:
        return asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_run())
