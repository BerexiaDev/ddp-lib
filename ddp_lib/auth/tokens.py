"""Shared validation for Core's DDP JWTs. Keycloak tokens stay in Auth."""
import time
import uuid
from flask import current_app

import jwt


def token_config(settings=None):
    """Validate settings loaded by the service; never read process environment."""
    settings = current_app.config if settings is None else settings
    secret = settings.get("SECRET_KEY")
    issuer = settings.get("DDP_TOKEN_ISSUER")
    audience = settings.get("DDP_TOKEN_AUDIENCE")
    if (not isinstance(secret, str) or len(secret.encode()) < 32
            or not isinstance(issuer, str) or not issuer.strip()
            or not isinstance(audience, str) or not audience.strip()):
        raise ValueError("SECRET_KEY must contain at least 32 bytes; DDP token issuer/audience must be nonempty")
    return secret, issuer, audience


def encode_token(subject, token_type, expires_at):
    secret, issuer, audience = token_config()
    return jwt.encode({
        "sub": str(subject), "token_type": token_type,
        "jti": uuid.uuid4().hex, "iat": int(time.time()),
        "exp": int(expires_at), "iss": issuer, "aud": audience,
    }, secret, algorithm="HS256")


def decode_token(token, token_type, *, allow_expired=False):
    """Validate a DDP token; only logout may accept expired credentials."""
    secret, issuer, audience = token_config()
    if not isinstance(token, str) or not token:
        raise jwt.InvalidTokenError("Missing token")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], issuer=issuer,
                            audience=audience, options={"verify_exp": not allow_expired, "require": [
                                "exp", "iat", "iss", "aud", "sub", "jti", "token_type"]})
        # Disabling expiry checks must not allow a malformed expiry claim.
        if allow_expired:
            int(claims["exp"])
    except (TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("Invalid token claims") from exc
    if claims["token_type"] != token_type or any(
        not isinstance(claims[name], str) or not claims[name]
        for name in ("sub", "jti")
    ):
        raise jwt.InvalidTokenError("Invalid token claims")
    return claims


def bearer_token(header):
    parts = (header or "").split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise jwt.InvalidTokenError("Provide a valid bearer token")
    return parts[1]
