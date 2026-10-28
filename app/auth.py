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
from fastapi.responses import HTMLResponse, RedirectResponse
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


# A regular login form also works in mobile browsers that do not show
# HTTP Basic authentication prompts. It uses the same admin credentials.
def _safe_next(value: str) -> str:
    return value if value in ("/", "/integrations") else "/integrations"


def _login_html(next_path: str, invalid: bool = False) -> HTMLResponse:
    from html import escape
    # Only a fixed, same-origin redirect path may be included in the form.
    target = escape(_safe_next(next_path), quote=True)
    error = '<p class="error" role="alert">Inloggegevens kloppen niet. Probeer het opnieuw.</p>' if invalid else ""
    html = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>SuperMerch — Inloggen</title>
<style>
:root{color-scheme:light;font-family:system-ui,-apple-system,sans-serif}
body{margin:0;min-height:100vh;background:#edf6fd;display:grid;place-items:center;padding:20px;box-sizing:border-box}
main{width:min(100%,400px);background:white;border-radius:20px;padding:32px;box-shadow:0 15px 45px #102f4d12}
h1{font-size:26px;margin:0 0 8px}p{color:#536276;line-height:1.5}label{display:block;margin:19px 0 6px;font-weight:650}
input{width:100%;box-sizing:border-box;padding:13px;font:inherit;border:1px solid #b8c8d8;border-radius:10px}
button{width:100%;background:#111;color:white;border:0;border-radius:10px;padding:15px;margin-top:24px;font:inherit;font-weight:650;cursor:pointer}
.error{color:#a52626;font-weight:600}small{color:#57677a;display:block;margin-top:18px}
</style></head><body><main>
<h1>SuperMerch Leadmachine</h1><p>Log in om Zoho Mail opnieuw te koppelen en je concepten klaar te zetten.</p>
""" + error + """
<form action="/login" method="post">
<input type="hidden" name="next" value='""" + target + """'/>
<label for="user">Gebruikersnaam</label><input id="user" name="username" autocomplete="username" required/>
<label for="pass">Wachtwoord</label><input id="pass" type="password" name="password" autocomplete="current-password" required/>
<button type="submit">Inloggen en verdergaan</button></form>
<small>De inloggegevens zijn dezelfde als voor je Leadmachine. Er worden geen mails verstuurd.</small>
</main></body></html>"""
    response = HTMLResponse(html, status_code=401 if invalid else 200)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@sso_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/integrations"):
    target = _safe_next(next)
    if cookie_is_valid(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse(url=target, status_code=303)
    return _login_html(target)


@sso_router.post("/login")
def login_with_password(username: str = Form(...), password: str = Form(...), next: str = Form("/integrations")):
    if (settings.app_password and settings.app_password != "change-me" and
        hmac.compare_digest(username, settings.app_username) and
        hmac.compare_digest(password, settings.app_password)):
        response = RedirectResponse(url=_safe_next(next), status_code=303)
        set_remember_cookie(response)
        response.headers["Cache-Control"] = "no-store"
        return response
    return _login_html(next, invalid=True)
