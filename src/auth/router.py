"""Публичный auth-роутер: register/login/refresh/oidc + me/logout.

Refresh-токен дополнительно кладётся в httpOnly+SameSite cookie (для браузера за single-origin
reverse-proxy); тело запроса с refresh_token остаётся для совместимости (Streamlit-посредник,
HttpBackend). login/register под ограничителем частоты по IP.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from src.auth import ratelimit
from src.auth.deps import bearer_token, get_auth, get_current_user
from src.auth.oidc import login_with_id_token
from src.auth.schemas import LoginReq, RefreshReq, RegisterReq
from src.auth.service import AuthService

if TYPE_CHECKING:
    from collections.abc import Callable

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(response: Response, auth: AuthService, token: str) -> None:
    """Положить refresh в httpOnly-cookie (path из cfg.cookie_path), если cookie включены и токен не пуст."""
    cfg = auth.cfg
    if not cfg.cookie_enabled or not token:
        return
    ss = cfg.cookie_samesite if cfg.cookie_samesite in ("lax", "strict", "none") else "lax"
    response.set_cookie(cfg.cookie_name, token, max_age=cfg.refresh_ttl, httponly=True,
                        secure=cfg.cookie_secure,
                        samesite=cast(Literal["lax", "strict", "none"], ss), path=cfg.cookie_path)


def _clear_refresh_cookie(response: Response, auth: AuthService) -> None:
    """Удалить refresh-cookie (тем же path, что и при установке, иначе браузер её не снимет)."""
    response.delete_cookie(auth.cfg.cookie_name, path=auth.cfg.cookie_path)


def _rate_limit(scope: str) -> Callable[..., None]:
    """Собрать зависимость-ограничитель частоты по IP для эндпоинта."""
    def dep(request: Request, auth: AuthService = Depends(get_auth)) -> None:
        cfg = auth.cfg
        ip = request.client.host if request.client else "unknown"
        if not ratelimit.allow(auth.cache, f"{scope}:{ip}",
                               cfg.rate_limit_attempts, cfg.rate_limit_window):
            raise HTTPException(status_code=429, detail="too many requests")
    return dep


@router.post("/register", dependencies=[Depends(_rate_limit("register"))])
def register(r: RegisterReq, auth: AuthService = Depends(get_auth)) -> dict:
    """Зарегистрировать пользователя по логину и паролю."""
    res = auth.register(r.login, r.password)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/login", dependencies=[Depends(_rate_limit("login"))])
def login(r: LoginReq, response: Response, auth: AuthService = Depends(get_auth)) -> dict:
    """Аутентифицировать по паролю, выдать пару токенов и httpOnly refresh-cookie."""
    res = auth.login_password(r.login, r.password)
    if "error" in res:
        raise HTTPException(status_code=401, detail=res["error"])
    _set_refresh_cookie(response, auth, res.get("refresh_token", ""))
    return res


@router.post("/refresh")
def refresh(request: Request, response: Response,
            r: RefreshReq | None = Body(default=None),
            auth: AuthService = Depends(get_auth)) -> dict:
    """Обновить пару токенов по refresh из тела запроса либо из cookie."""
    token = (r.refresh_token if r else None) or request.cookies.get(auth.cfg.cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="missing refresh token")
    res = auth.refresh(token)
    if "error" in res:
        raise HTTPException(status_code=401, detail=res["error"])
    _set_refresh_cookie(response, auth, res.get("refresh_token", ""))
    return res


@router.post("/oidc/{provider}")
def oidc(provider: str, response: Response, id_token: str = Body(..., embed=True),
         auth: AuthService = Depends(get_auth)) -> dict:
    """Войти через OIDC-провайдера по id_token."""
    res = login_with_id_token(auth, provider, id_token)
    if "error" in res:
        raise HTTPException(status_code=401, detail=res["error"])
    _set_refresh_cookie(response, auth, res.get("refresh_token", ""))
    return res


@router.post("/oidc/{provider}/callback")
def oidc_callback(provider: str, request: Request, credential: str = Form(...),
                  g_csrf_token: str = Form(default=""),
                  auth: AuthService = Depends(get_auth)) -> Response:
    """Принять GIS-callback (ux_mode=redirect): по credential выдать сессию и увести на фронт.

    Браузер постит сюда credential (id_token) и g_csrf_token. Проверяем CSRF (double-submit cookie),
    логиним по id_token, ставим refresh-cookie и редиректим на корень фронта - дальше сессию
    подхватывает фронт по куке (как при F5).
    """
    cookie_csrf = request.cookies.get("g_csrf_token")
    if not cookie_csrf or cookie_csrf != g_csrf_token:
        raise HTTPException(status_code=400, detail="oidc csrf check failed")
    res = login_with_id_token(auth, provider, credential)
    if "error" in res:
        raise HTTPException(status_code=401, detail=res["error"])
    redirect = RedirectResponse(url="/", status_code=303)
    _set_refresh_cookie(redirect, auth, res.get("refresh_token", ""))
    return redirect


@router.get("/forward-auth")
def forward_auth(request: Request, auth: AuthService = Depends(get_auth)) -> Response:
    """auth_request для reverse-proxy: 200 если по refresh-куке пользователь admin, иначе 401.

    Источник доступа к внешним панелям - роль аккаунта в нашей БД (без IdP). При 200 отдаём
    X-Auth-User/X-Auth-Role: nginx копирует их в проксируемый запрос (auth_request_set), а Grafana
    через auth.proxy опознаёт пользователя по ним - вместо безличного anonymous-admin. Тело пустое.
    """
    user = auth.resolve_refresh(request.cookies.get(auth.cfg.cookie_name))
    if not (user and user.get("role") == "admin"):
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Auth-User": user.get("login") or "",
                                              "X-Auth-Role": "Admin"})


# Требуют валидного access.
@router.get("/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    """Вернуть профиль текущего пользователя."""
    return user


@router.post("/logout")
def logout(response: Response, token: str | None = Depends(bearer_token),
           user: dict = Depends(get_current_user),
           auth: AuthService = Depends(get_auth)) -> dict:
    """Завершить сессию: снять access, отозвать refresh пользователя, удалить cookie."""
    res = auth.logout(token)
    _clear_refresh_cookie(response, auth)
    return res
