"""OIDC-готовность: верификация id_token провайдера → (provider, subject, claims).

Полный поток (Authorization Code / id_token) специфичен для провайдера и требует его JWKS.
Здесь - точка расширения: `verify_id_token` по конфигу провайдера достаёт проверенные claims,
из которых берём `sub`. Дальше `AuthService.login_oidc(provider, subject, claims)` находит/создаёт
пользователя по таблице identities и выдаёт токены (как при обычном логине).

Конфиг провайдера (config.yaml → auth.oidc):
  google:   { jwks_url: "https://www.googleapis.com/oauth2/v3/certs", audience: "<client_id>",
              issuer: "https://accounts.google.com" }
  keycloak: { jwks_url: "https://kc/realms/<r>/protocol/openid-connect/certs", audience, issuer }
"""
import jwt

from src.auth.service import AuthService


def verify_id_token(provider_cfg: dict, id_token: str) -> dict:
    """Проверить подпись id_token по JWKS провайдера и вернуть claims (с `sub`)."""
    jwks_url = provider_cfg.get("jwks_url")
    if not jwks_url:
        raise ValueError("OIDC provider not configured (no jwks_url)")
    signing_key = jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(id_token).key
    return jwt.decode(
        id_token, signing_key, algorithms=["RS256"],
        audience=provider_cfg.get("audience"), issuer=provider_cfg.get("issuer"),
    )


def login_with_id_token(auth: AuthService, provider: str, id_token: str) -> dict:
    """Верифицировать id_token и выдать наши токены через AuthService."""
    provider_cfg = (auth.cfg.oidc or {}).get(provider)
    if not provider_cfg:
        return {"error": f"unknown oidc provider: {provider}"}
    try:
        claims = verify_id_token(provider_cfg, id_token)
    except Exception as e:  # подпись/issuer/audience не сошлись
        return {"error": f"oidc verification failed: {e}"}
    sub = claims.get("sub")
    if not sub:
        return {"error": "oidc token without sub"}
    return auth.login_oidc(provider, sub, claims)
