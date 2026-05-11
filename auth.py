"""JWT 签发/验证 — 与 followup-agent 共用同一个 JWT_SECRET。"""

import os
import time

import jwt  # PyJWT

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-to-a-random-string")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 天


def create_token(phone: str, openid: str, company: str | None = None) -> str:
    payload = {
        "phone": phone,
        "openid": openid,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRE_SECONDS,
    }
    if company:
        payload["company"] = company
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
