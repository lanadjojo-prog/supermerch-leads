"""SuperMerch protected admin login with 90-day secure cookie and Operations SSO."""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from .config import settings

sso_router = APIRouter()
SESSION_COOKIE = "sm_leads_remember"
SESSION_MAX_AGE = 90 * 86400


def _session_signature(timestamp: str, nonce: str) -> str:
    key = hashlib.sha256(
        ("sm-leads-session:v1|" + settings.app_password + "|" + settings.session_secret).encode()
    ).digest()
    return hmac.new(key, (timestamp + "|" + nonce).encode(), hashlib.sha256).hexdigest()


def new_session_token() -> str:
    from secrets import token_urlsafe
    timestamp = str(int(time.time()))
    nonce = token_urlsafe(18)
    return timestamp + "." + nonce + "." + _session_signature(timestamp, nonce)


def cookie_is_valid(token: str) -> bool:
    try:
        timestamp, nonce, signature = token.split(".")
        age = time.time() - int(timestamp)
        if age < -60 or age > SESSION_MAX_AGE or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", nonce):
            return False
        return hmac.compare_digest(signature, _session_signature(timestamp, nonce))
    except (ValueError, TypeError, OverflowError):
        return False


def set_remember_cookie(response):
    response.set_cookie(
        key=SESSION_COOKIE, value=new_session_token(), httponly=True,
        secure=True, samesite="lax", max_age=SESSION_MAX_AGE, path="/",
    )


def require_basic_auth(request: Request) -> None:
    if not settings.app_password or settings.app_password == "change-me":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APP_PASSWORD is nog niet ingesteld op Render.",
        )
    if cookie_is_valid(request.cookies.get(SESSION_COOKIE, "")):
        return
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
            if (hmac.compare_digest(username, settings.app_username)
                    and hmac.compare_digest(password, settings.app_password)):
                request.state.sm_basic_authenticated = True
                return
        except (ValueError, UnicodeDecodeError, binascii.Error):
            pass
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": 'Basic realm="SuperMerch Leads", charset="UTF-8"'},
    )


@sso_router.post("/auth/sso")
def login_from_operations(ticket: str = Form(...)):
    """Issue private lead dashboard cookie only from signed, 60-second Operations grants."""
    shared = os.environ.get("SYSTEM_MAP_BRIDGE_TOKEN", "")
    if len(shared) < 40 or len(ticket) > 1024:
        raise HTTPException(status_code=403, detail="Beveiligde toegang niet geconfigureerd")
    try:
        encoded, supplied = ticket.split(".")
        expected = hmac.new(shared.encode(), ("sm-sso:v1|" + encoded).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature")
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claim = json.loads(raw)
        now = int(time.time())
        if (not isinstance(claim, dict) or
                not isinstance(claim.get("exp"), int) or
                claim["exp"] < now or claim["exp"] > now + 60 or
                not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", str(claim.get("nonce", "")))):
            raise ValueError("expired")
        target = claim.get("next")
        if target not in ("/", "/instagram/"):
            raise ValueError("invalid destination")
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=403, detail="Aanmeldlink ongeldig of verlopen")
    response = RedirectResponse(url=target, status_code=303)
    set_remember_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
