from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr

from backend.dependencies import SettingsDep
from backend.models.auth import Account, AuthError, Principal, Role
from backend.services.auth import AuthService, TokenBundle

router = APIRouter(prefix="/auth", tags=["auth"])
admin_router = APIRouter(prefix="/admin/users", tags=["accounts"])
bearer = HTTPBearer(auto_error=False)
COOKIE_PREFIX = "sf_refresh_"
COOKIE_PATH = "/api/auth"


def cookie_name(sid: UUID) -> str:
    return f"{COOKIE_PREFIX}{sid.hex}"


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


AuthDep = Annotated[AuthService, Depends(auth_service)]
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def require_origin(request: Request, settings: SettingsDep) -> None:
    if request.headers.get("origin") not in settings.auth.allowed_origins:
        raise AuthError("ORIGIN_REJECTED", "请求来源不被允许", 403)


def session_id(request: Request, *, required: bool = True) -> UUID | None:
    raw = request.headers.get("X-Session-ID")
    if raw:
        try:
            return UUID(raw)
        except ValueError:
            pass
    if required or raw:
        raise AuthError("SESSION_INVALID", "登录会话无效")
    return None


async def current_principal(
    request: Request, response: Response, service: AuthDep, credentials: CredentialsDep
) -> Principal:
    if not credentials:
        raise AuthError("LOGIN_REQUIRED", "请先登录")
    origin = request.headers.get("origin")
    if origin and origin not in service.settings.allowed_origins:
        raise AuthError("ORIGIN_REJECTED", "请求来源不被允许", 403)
    principal = await service.authenticate(credentials.credentials)
    if request.headers.get("X-Session-ID") and session_id(request) != principal.session_id:
        raise AuthError("SESSION_INVALID", "登录会话不匹配")
    response.headers["X-Auth-Session"] = str(principal.session_id)
    response.headers["Cache-Control"] = "no-store"
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_admin(principal: PrincipalDep) -> Principal:
    if principal.account.role != "admin":
        raise AuthError("FORBIDDEN", "需要管理员权限", 403)
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


class UserView(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime

    @classmethod
    def of(cls, user: Account):
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
        )


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: SecretStr = Field(min_length=1, max_length=128)


class CreateUserBody(LoginBody):
    display_name: str = Field(min_length=1, max_length=64)
    role: Role = "user"


class PasswordBody(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


class LoginView(BaseModel):
    access_token: str
    token_type: str = "bearer"
    session_id: UUID
    user: UserView


class SavedLoginView(BaseModel):
    session_id: UUID
    user: UserView


def set_refresh(response: Response, service: AuthService, bundle: TokenBundle) -> LoginView:
    seconds = max(0, int((bundle.refresh_expires - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        cookie_name(bundle.session_id),
        bundle.refresh_token,
        max_age=seconds,
        expires=bundle.refresh_expires,
        path=COOKIE_PATH,
        httponly=True,
        secure=service.settings.cookie_secure,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return LoginView(
        access_token=bundle.access_token,
        session_id=bundle.session_id,
        user=UserView.of(bundle.user),
    )


def clear_refresh(response: Response, sid: UUID, secure: bool) -> None:
    response.delete_cookie(
        cookie_name(sid), path=COOKIE_PATH, httponly=True, secure=secure, samesite="strict"
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("/login", response_model=LoginView, dependencies=[Depends(require_origin)])
async def login(body: LoginBody, request: Request, response: Response, service: AuthDep):
    await service.check_login_budget(
        request.client.host if request.client else "unknown", body.username
    )
    old_sid = session_id(request, required=False)
    old_cookie = request.cookies.get(cookie_name(old_sid)) if old_sid else None
    replaced = service.replaced_session(old_sid, old_cookie)
    existing = sum(name.startswith(COOKIE_PREFIX) for name in request.cookies)
    if existing - int(replaced is not None) >= service.settings.browser_session_limit:
        raise AuthError("BROWSER_SESSION_LIMIT", "此浏览器登录会话较多，请先退出不使用的账号", 409)
    bundle = await service.login(body.username, body.password.get_secret_value(), replaced)
    if replaced:
        clear_refresh(response, replaced[0], service.settings.cookie_secure)
    return set_refresh(response, service, bundle)


@router.post("/refresh", response_model=LoginView, dependencies=[Depends(require_origin)])
async def refresh(request: Request, response: Response, service: AuthDep):
    sid = session_id(request)
    token = request.cookies.get(cookie_name(sid))
    try:
        if not token:
            raise AuthError("LOGIN_REQUIRED", "请重新登录")
        bundle = await service.refresh(sid, token)
    except AuthError:
        request.state.clear_refresh_sid = sid
        raise
    return set_refresh(response, service, bundle)


@router.post("/logout", status_code=204, dependencies=[Depends(require_origin)])
async def logout(
    request: Request, response: Response, service: AuthDep, credentials: CredentialsDep
):
    sid = session_id(request)
    await service.logout(
        sid, request.cookies.get(cookie_name(sid)), credentials.credentials if credentials else None
    )
    clear_refresh(response, sid, service.settings.cookie_secure)


@router.get("/me", response_model=UserView)
async def me(principal: PrincipalDep):
    return UserView.of(principal.account)


@router.post(
    "/sessions", response_model=list[SavedLoginView], dependencies=[Depends(require_origin)]
)
async def available_sessions(request: Request, response: Response, service: AuthDep):
    candidates = []
    for name, token in request.cookies.items():
        if not name.startswith(COOKIE_PREFIX):
            continue
        try:
            candidates.append((UUID(name[len(COOKIE_PREFIX) :]), token))
        except ValueError:
            continue
    response.headers["Cache-Control"] = "no-store"
    return [
        SavedLoginView(session_id=item.session_id, user=UserView.of(item.account))
        for item in await service.available_logins(candidates)
    ]


@router.post("/password", status_code=204)
async def change_password(
    body: PasswordBody, principal: PrincipalDep, service: AuthDep, response: Response
):
    await service.change_password(
        principal, body.current_password.get_secret_value(), body.new_password.get_secret_value()
    )
    clear_refresh(response, principal.session_id, service.settings.cookie_secure)


@admin_router.get("", response_model=list[UserView])
async def list_users(
    principal: AdminDep,
    service: AuthDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return [UserView.of(user) for user in await service.store.list_users(limit, offset)]


@admin_router.post("", response_model=UserView, status_code=201)
async def create_user(body: CreateUserBody, principal: AdminDep, service: AuthDep):
    return UserView.of(
        await service.create_user(
            body.username, body.display_name, body.role, body.password.get_secret_value()
        )
    )
