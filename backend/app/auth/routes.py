"""Durable login, refresh rotation, logout, and authenticated identity endpoints."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, select, update

from app.auth.security import AuthorizationError, JwtService, PasswordService, Principal, Role, require_approval_authority, require_role
from app.db import models


router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
FAILED_LOGIN_LIMIT = 5
LOCKOUT_DURATION = timedelta(minutes=15)


def jwt_service() -> JwtService:
    """Build the signer from an explicit deployment secret; never generate one."""
    key = os.environ.get("JWT_SIGNING_KEY")
    if not key:
        raise RuntimeError("JWT_SIGNING_KEY must be explicitly configured before application startup.")
    return JwtService(key)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class ApprovalRequest(BaseModel):
    requested_by_user_id: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _audit(request: Request, event_type: str, actor_user_id: UUID | None, payload: dict[str, object]) -> None:
    await request.app.state.control_plane.audit.create(actor_user_id=actor_user_id, optimization_run_id=None, event_type=event_type, event_payload=payload)


async def current_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> Principal:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    try:
        principal = jwt_service().verify(credentials.credentials)
        user = await request.app.state.control_plane.users.get(principal.user_id)
        if user is None or user["status"] != "ACTIVE":
            raise AuthorizationError("User is disabled or unavailable.")
        return Principal(user_id=str(user["id"]), role=Role(str(user["role"])))
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.") from error


@router.post("/auth/login")
async def login(login_request: LoginRequest, request: Request) -> dict[str, str]:
    engine = request.app.state.control_plane.users._engine
    now = datetime.now(timezone.utc)
    failed_actor: UUID | None = None
    failed_payload: dict[str, object] | None = None
    user = None
    async with engine.begin() as connection:
        result = await connection.execute(select(models.User.__table__).where(models.User.email == str(login_request.email).lower()).with_for_update())
        user = result.mappings().one_or_none()
        if user is None or user["status"] != "ACTIVE" or (user["locked_until"] is not None and user["locked_until"] > now):
            failed_payload = {"email": str(login_request.email).lower()}
        elif not PasswordService().verify_password(str(user["password_hash"]), login_request.password):
            failures = int(user["failed_login_count"]) + 1
            locked_until = now + LOCKOUT_DURATION if failures >= FAILED_LOGIN_LIMIT else None
            await connection.execute(update(models.User.__table__).where(models.User.id == user["id"]).values(failed_login_count=failures, locked_until=locked_until))
            failed_actor = user["id"]
            failed_payload = {"failures": failures, "locked": locked_until is not None}
        else:
            await connection.execute(update(models.User.__table__).where(models.User.id == user["id"]).values(failed_login_count=0, locked_until=None))
    if failed_payload is not None:
        await _audit(request, "AUTH_LOGIN_FAILED", failed_actor, failed_payload)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")
    assert user is not None
    principal = Principal(user_id=str(user["id"]), role=Role(str(user["role"])))
    refresh_id = str(uuid4())
    service = jwt_service()
    refresh_token = service.issue(principal, "refresh", refresh_id)
    await request.app.state.control_plane.refresh_tokens.create(user_id=user["id"], token_id=refresh_id, token_hash=_token_hash(refresh_token), expires_at=now + timedelta(days=7), revoked_at=None)
    await _audit(request, "AUTH_LOGIN_SUCCEEDED", user["id"], {})
    return {"access_token": service.issue(principal), "refresh_token": refresh_token, "token_type": "bearer"}


@router.post("/auth/refresh")
async def refresh(body: RefreshRequest, request: Request) -> dict[str, str]:
    try:
        service = jwt_service()
        claims = service.decode_claims(body.refresh_token, expected_token_type="refresh")
        token_id = str(claims["jti"])
    except Exception as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.") from error
    engine = request.app.state.control_plane.refresh_tokens._engine
    now = datetime.now(timezone.utc)
    async with engine.begin() as connection:
        result = await connection.execute(select(models.RefreshToken.__table__).where(and_(models.RefreshToken.token_id == token_id, models.RefreshToken.token_hash == _token_hash(body.refresh_token))).with_for_update())
        record = result.mappings().one_or_none()
        if record is None or record["revoked_at"] is not None or record["expires_at"] <= now:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")
        user_result = await connection.execute(select(models.User.__table__).where(models.User.id == record["user_id"]))
        user = user_result.mappings().one_or_none()
        if user is None or user["status"] != "ACTIVE":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")
        await connection.execute(update(models.RefreshToken.__table__).where(models.RefreshToken.id == record["id"]).values(revoked_at=now))
    active_principal = Principal(user_id=str(user["id"]), role=Role(str(user["role"])))
    new_id = str(uuid4())
    new_refresh = service.issue(active_principal, "refresh", new_id)
    await request.app.state.control_plane.refresh_tokens.create(user_id=user["id"], token_id=new_id, token_hash=_token_hash(new_refresh), expires_at=now + timedelta(days=7), revoked_at=None)
    await _audit(request, "AUTH_REFRESH_ROTATED", user["id"], {})
    return {"access_token": service.issue(active_principal), "refresh_token": new_refresh, "token_type": "bearer"}


@router.post("/auth/logout")
async def logout(body: RefreshRequest, request: Request) -> dict[str, str]:
    try:
        claims = jwt_service().decode_claims(body.refresh_token, expected_token_type="refresh")
    except AuthorizationError:
        return {"status": "logged_out"}
    engine = request.app.state.control_plane.refresh_tokens._engine
    async with engine.begin() as connection:
        await connection.execute(update(models.RefreshToken.__table__).where(and_(models.RefreshToken.token_id == str(claims["jti"]), models.RefreshToken.token_hash == _token_hash(body.refresh_token), models.RefreshToken.revoked_at.is_(None))).values(revoked_at=datetime.now(timezone.utc)))
    return {"status": "logged_out"}


@router.get("/auth/me")
async def me(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, str]:
    user = await request.app.state.control_plane.users.get(principal.user_id)
    return {"user_id": principal.user_id, "role": principal.role.value, "email": str(user["email"]) if user else ""}


@router.post("/operations/execute")
async def execute_operator_operation(principal: Principal = Depends(current_principal)) -> dict[str, str]:
    try:
        require_role(principal, Role.OPERATOR, Role.ADMIN)
    except AuthorizationError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return {"status": "authorized"}


@router.post("/approvals/{request_id}")
async def approve_request(request_id: str, approval_request: ApprovalRequest, principal: Principal = Depends(current_principal)) -> dict[str, str]:
    try:
        require_approval_authority(principal, approval_request.requested_by_user_id)
    except AuthorizationError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    return {"request_id": request_id, "status": "approved"}


def issue_test_token(principal: Principal) -> str:
    """Issue tokens only for isolated cryptographic unit tests."""
    return JwtService("test-signing-key-for-isolated-unit-tests").issue(principal)
