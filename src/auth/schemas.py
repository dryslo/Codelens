"""Pydantic-схемы запросов авторизации."""
from pydantic import BaseModel


class RegisterReq(BaseModel):
    """Тело запроса регистрации."""

    login: str
    password: str


class LoginReq(BaseModel):
    """Тело запроса логина по паролю."""

    login: str
    password: str


class RefreshReq(BaseModel):
    """Тело запроса обновления токенов."""

    refresh_token: str


class SetRoleReq(BaseModel):
    """Тело запроса смены роли пользователя."""

    role: str
