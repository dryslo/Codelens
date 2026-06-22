"""Конфигурация авторизации."""
from dataclasses import dataclass, field


@dataclass
class AuthConfig:
    """Параметры авторизации (токены, секреты, OIDC)."""

    enabled: bool = False
    secret: str = "dev-insecure-change-me"
    alg: str = "HS256"
    access_ttl: int = 900          # сек, access-токен (живёт в кэше)
    refresh_ttl: int = 2592000     # сек, refresh-токен (живёт в БД)
    oidc: dict = field(default_factory=dict)
    cookie_name: str = "codelens_rt"
    cookie_enabled: bool = True    # ставить refresh в httpOnly-cookie на ответах auth
    cookie_secure: bool = False    # true в prod (HTTPS); иначе браузер не сохранит Secure-cookie
    cookie_samesite: str = "lax"   # lax | strict | none
    cookie_path: str = "/"         # scope куки. "/" нужен, чтобы forward-auth видел её и на /grafana,
                                   # /pgadmin и пр. (гейт панелей по refresh-куке); сузить ломает гейт
    rate_limit_attempts: int = 10  # попыток login/register на IP за окно (0 - выключено)
    rate_limit_window: int = 60    # окно ограничителя частоты, сек

    @classmethod
    def from_cfg(cls, cfg: dict) -> "AuthConfig":
        """Построить конфиг из секции auth словаря конфигурации."""
        a = cfg.get("auth") or {}
        return cls(
            enabled=str(a.get("enabled", "false")).lower() == "true",
            secret=a.get("jwt_secret", "dev-insecure-change-me"),
            alg=a.get("jwt_alg", "HS256"),
            access_ttl=int(a.get("access_ttl", 900)),
            refresh_ttl=int(a.get("refresh_ttl", 2592000)),
            oidc=a.get("oidc") or {},
            cookie_name=a.get("cookie_name", "codelens_rt"),
            cookie_enabled=str(a.get("cookie_enabled", "true")).lower() == "true",
            cookie_secure=str(a.get("cookie_secure", "false")).lower() == "true",
            cookie_samesite=str(a.get("cookie_samesite", "lax")).lower(),
            cookie_path=a.get("cookie_path", "/"),
            rate_limit_attempts=int(a.get("rate_limit_attempts", 10)),
            rate_limit_window=int(a.get("rate_limit_window", 60)),
        )
