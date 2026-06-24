"""Сессия фронта: composition root + cookie-персист + логин/refresh/logout.

Ctx - единый контекст (backend/auth/cfg/policy/user), прокидывается во вкладки.
Refresh-токен живёт в cookie (codelens_rt); access - в st.session_state + Bearer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import streamlit as st
from streamlit_cookies_controller import CookieController

from src.factory import build, load_config

if TYPE_CHECKING:
    from src.factory import Components

REFRESH_COOKIE = "codelens_rt"
_cookies = CookieController()


@dataclass
class Ctx:
    """Контекст сессии: comp/backend/auth/cfg/policy и текущий пользователь."""

    comp: Components
    backend: object
    auth: object
    cfg: dict
    auth_on: bool
    user_id: str = "anon"
    role: str = "admin"
    policy: object = None


@st.cache_resource
def _build_comp() -> Components:
    """Строит composition root один раз на процесс (кэш Streamlit)."""
    return build()


def get_context() -> Ctx:
    """Собирает контекст сессии: comp, конфиг и признак включённой авторизации."""
    comp = _build_comp()
    cfg = comp.cfg or load_config()
    auth_on = str((cfg.get("auth") or {}).get("enabled", "false")).lower() == "true"
    return Ctx(comp=comp, backend=comp.backend, auth=comp.auth, cfg=cfg, auth_on=auth_on)


def load_policy(ctx: Ctx) -> None:
    """Загружает политику флагов retrieval в контекст."""
    from src.retrieval.flags import FlagsPolicy
    ctx.policy = FlagsPolicy(**ctx.backend.flag_policy())


def _refresh_ttl(ctx: Ctx) -> int:
    return int((ctx.cfg.get("auth") or {}).get("refresh_ttl", 2592000))


# --- cookie-операции (до первого round-trip стор=None, методы кидают, поэтому в try) ---
def _wait_for_cookies() -> None:
    """Ждёт, пока cookie-компонент отдаст значения.

    Иначе set/get падают на None-сторе, а логин не переживает F5.
    """
    try:
        loaded = _cookies.getAll()
    except Exception:  # noqa: BLE001
        loaded = None
    if loaded is None and st.session_state.get("_cookie_waits", 0) < 5:
        st.session_state["_cookie_waits"] = st.session_state.get("_cookie_waits", 0) + 1
        st.caption("Загрузка…")
        st.stop()


def _remove_refresh_cookie() -> None:
    try:
        _cookies.remove(REFRESH_COOKIE)
    except Exception:  # noqa: BLE001 - cookie ещё не загружена/уже отсутствует
        pass


def _read_refresh_cookie() -> str | None:
    try:
        v = _cookies.get(REFRESH_COOKIE)
    except Exception:  # noqa: BLE001
        v = None
    if v:
        return v
    ctx_cookies = getattr(st.context, "cookies", None) or {}
    return ctx_cookies.get(REFRESH_COOKIE)


def _cookie_secure(ctx: Ctx) -> bool:
    return str((ctx.cfg.get("auth") or {}).get("cookie_secure", "false")).lower() == "true"


def _apply_session(ctx: Ctx, res: dict) -> None:
    """Сохраняет выданную пару токенов: память + Bearer + cookie (refresh)."""
    st.session_state.auth = res
    if hasattr(ctx.backend, "token"):
        ctx.backend.token = res["access_token"]
    if res.get("refresh_token"):
        try:
            # SameSite=Strict (дефолт контроллера) против CSRF; Secure - в prod (HTTPS).
            # path="/" - чтобы кука уходила и на /grafana, /adminer и пр. (гейт панелей по forward-auth).
            _cookies.set(REFRESH_COOKIE, res["refresh_token"], max_age=_refresh_ttl(ctx),
                         same_site="strict", secure=_cookie_secure(ctx), path="/")
        except Exception:  # noqa: BLE001 - стор cookie ещё не готов; сессия в памяти живёт
            pass


def _clear_session(ctx: Ctx) -> None:
    st.session_state.pop("auth", None)
    _remove_refresh_cookie()
    if hasattr(ctx.backend, "token"):
        ctx.backend.token = None


def _google_signin(ctx: Ctx) -> None:
    """Ссылка «Войти через Google». Видна, если задан clientId.

    Не GIS-виджет: тот живёт в sandboxed iframe Streamlit без allow-top-navigation, поэтому не может
    увести верхнее окно на Google. Вместо него - обычная ссылка верхнего уровня на OAuth-эндпоинт
    (`response_type=id_token`, `response_mode=form_post`). Google form_post'ит id_token на backend
    login_uri (`/auth/oidc/google/callback`), тот ставит refresh-куку и редиректит на /; сессию фронт
    подхватывает по куке (как при F5).
    """
    g = ((ctx.cfg.get("auth") or {}).get("oidc") or {}).get("google") or {}
    cid, uri = g.get("clientId"), g.get("login_uri")
    if not cid or not uri:
        return
    import secrets
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": uri,
        "response_type": "id_token",
        "scope": "openid email profile",
        "response_mode": "form_post",
        "nonce": secrets.token_urlsafe(16),   # Google требует nonce для id_token-потока
        "prompt": "select_account",
    })
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + params
    st.divider()
    st.link_button("Войти через Google", auth_url, use_container_width=True)


def _login_screen(ctx: Ctx) -> None:
    st.title("CodeLens - вход")
    lt, rt = st.tabs(["Вход", "Регистрация"])
    with lt:
        lg = st.text_input("Логин", key="lg_login")
        pw = st.text_input("Пароль", type="password", key="lg_pw")
        if st.button("Войти"):
            res = ctx.auth.login_password(lg, pw) if ctx.auth is not None else ctx.backend.login(lg, pw)
            if res.get("access_token"):
                _apply_session(ctx, res)
                st.rerun()
            else:
                st.error(res.get("error", "Неверный логин или пароль"))
        _google_signin(ctx)
    with rt:
        rl = st.text_input("Логин", key="rg_login")
        rp = st.text_input("Пароль", type="password", key="rg_pw")
        if st.button("Зарегистрироваться"):
            res = ctx.auth.register(rl, rp) if ctx.auth is not None else ctx.backend.register(rl, rp)
            if res.get("ok"):
                st.success("Готово - теперь войдите.")
            else:
                st.error(res.get("error", "Не удалось зарегистрироваться"))


def ensure_authenticated(ctx: Ctx) -> None:
    """Логин-гейт: без auth - anon=admin; иначе cookie-refresh или экран входа (st.stop)."""
    if not ctx.auth_on:
        ctx.user_id, ctx.role = "anon", "admin"
        return
    _wait_for_cookies()
    if "auth" not in st.session_state:
        rt = _read_refresh_cookie()
        if rt:
            res = ctx.auth.refresh(rt) if ctx.auth is not None else ctx.backend.refresh(rt)
            if res.get("access_token"):
                _apply_session(ctx, res)        # cookie обновляется ротированным refresh
            else:
                _remove_refresh_cookie()        # протух или отозван
        if "auth" not in st.session_state:
            _login_screen(ctx)
            st.stop()
    a = st.session_state["auth"]
    ctx.user_id, ctx.role = a["user"]["user_id"], a["user"]["role"]
    if hasattr(ctx.backend, "token"):
        ctx.backend.token = a["access_token"]


def render_logout(ctx: Ctx) -> None:
    """Профиль и «Выйти» внизу сайдбара (вызывать последним)."""
    if not ctx.auth_on or "auth" not in st.session_state:
        return
    a = st.session_state["auth"]
    with st.sidebar:
        st.divider()
        st.caption(f"👤 {a['user']['login']} · {ctx.role}")
        if st.button("Выйти", key="logout_btn"):
            if ctx.auth is not None:
                ctx.auth.logout(a["access_token"])
            elif hasattr(ctx.backend, "logout"):
                ctx.backend.logout()
            _clear_session(ctx)
            st.rerun()
