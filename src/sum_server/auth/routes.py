"""Auth routes: login, logout, me."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, status

from sum_server.auth import service as svc
from sum_server.auth.schemas import LoginRequest, MeResponse, TokenResponse
from sum_server.core.db import SessionDep
from sum_server.core.deps import UserActor
from sum_server.core.errors import AuthError
from sum_server.users.service import get_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: SessionDep,
) -> TokenResponse:
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    async with session.begin():
        raw, sess = await svc.login(
            session,
            email=payload.email,
            password=payload.password,
            ip=ip,
            user_agent=ua,
        )
    return TokenResponse(access_token=raw, expires_at=sess.expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    async with session.begin():
        await svc.logout(session, raw_token=token)


@router.get("/me", response_model=MeResponse)
async def me(actor: UserActor, session: SessionDep) -> MeResponse:
    assert actor.id is not None
    user = await get_user(session, actor.id)
    if user is None:
        raise AuthError("user not found for token")
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        created_at=user.created_at,
    )
