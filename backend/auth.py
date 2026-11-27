import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException

AUTH_SECRET = (os.getenv("JWT_SECRET") or os.getenv("AUTH_SECRET") or "zapit-host-session-secret-2026").encode("utf-8")
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def hash_password(password: str, salt: Optional[str] = None) -> str:
    clean_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        clean_salt.encode("utf-8"),
        120_000,
    ).hex()
    return f"{clean_salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, _ = stored_hash.split("$", 1)
    except ValueError:
        return False
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def create_session_token(email: str, name: str) -> str:
    payload = {
        "email": email.strip().lower(),
        "name": name.strip() or "Instructor",
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    raw_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(raw_json).decode("ascii")
    sig = hmac.new(AUTH_SECRET, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64_payload}.{sig}"


def decode_session_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        b64_payload, sig = token.strip().split(".", 1)
        expected_sig = hmac.new(AUTH_SECRET, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(base64.urlsafe_b64decode(b64_payload.encode("ascii")).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


async def require_host_auth(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Please sign in as a Host/Teacher to continue.")
    token = authorization.split(" ", 1)[1].strip()
    user = decode_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    return user