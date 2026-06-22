"""FastAPI-зависимости авторизации.

`require_user` / `require_admin` навешиваются на ГРУППЫ роутеров (общая зависимость),
а не на каждый эндпоинт по отдельности.
"""
from fastapi import Depends, Header, HTTPException, Request

from src.auth.service import AuthService


def get_auth(request: Request) -> AuthService:
    """Вернуть AuthService из состояния приложения."""
    return request.app.state.auth


def bearer_token(authorization: str | None = Header(default=None)) -> str | None:
    """Извлечь bearer-токен из заголовка Authorization."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:]
    return None


def get_current_user(token: str | None = Depends(bearer_token),
                     auth: AuthService = Depends(get_auth)) -> dict:
    """Вернуть текущего пользователя по access-токену или 401."""
    user = auth.resolve_access(token)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def require_user(user: dict = Depends(get_current_user)) -> dict:
    """Зависимость: требовать валидного пользователя."""
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Зависимость: требовать роль admin, иначе 403."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return user
