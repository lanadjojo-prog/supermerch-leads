from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import time
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import MailIntegration

PROVIDER = "zoho"
SCOPES = "ZohoMail.accounts.READ,ZohoMail.messages.CREATE"


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.session_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def configured() -> bool:
    return bool(settings.zoho_client_id and settings.zoho_client_secret)


def connected(db: Session) -> bool:
    return db.scalar(select(MailIntegration).where(MailIntegration.provider == PROVIDER)) is not None


def make_state() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(settings.session_secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def validate_state(state: str, max_age: int = 900) -> bool:
    try:
        ts, sig = state.split(".", 1)
        expected = hmac.new(settings.session_secret.encode(), ts.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected) and abs(time.time() - int(ts)) <= max_age
    except Exception:
        return False


def authorization_url(redirect_uri: str) -> str:
    if not configured():
        raise RuntimeError("Zoho client is nog niet geconfigureerd.")
    query = urlencode({
        "scope": SCOPES,
        "client_id": settings.zoho_client_id,
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": redirect_uri,
        "prompt": "consent",
        "state": make_state(),
    })
    return f"{settings.zoho_accounts_base}/oauth/v2/auth?{query}"


def _token_request(data: dict) -> dict:
    with httpx.Client(timeout=20) as client:
        response = client.post(f"{settings.zoho_accounts_base}/oauth/v2/token", data=data)
        response.raise_for_status()
        payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"Zoho OAuth fout: {payload.get('error')}")
    return payload


def exchange_code(code: str, redirect_uri: str) -> dict:
    return _token_request({
        "code": code,
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


def refresh_access_token(refresh_token: str) -> str:
    payload = _token_request({
        "refresh_token": refresh_token,
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        "grant_type": "refresh_token",
    })
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Zoho gaf geen access token terug.")
    return token


def _headers(access_token: str) -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Zoho-oauthtoken {access_token}",
    }


def get_accounts(access_token: str) -> list[dict]:
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{settings.zoho_mail_base}/api/accounts", headers=_headers(access_token))
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") or []
    return data if isinstance(data, list) else [data]


def _account_email(account: dict) -> str:
    for key in ("primaryEmailAddress", "emailAddress", "mailboxAddress"):
        value = account.get(key)
        if isinstance(value, str) and "@" in value:
            return value
    send_details = account.get("sendMailDetails")
    if isinstance(send_details, list):
        for item in send_details:
            for key in ("fromAddress", "emailAddress"):
                value = item.get(key)
                if isinstance(value, str) and "@" in value:
                    return value
    return ""


def _account_id(account: dict) -> str:
    for key in ("accountId", "accountID", "id"):
        if account.get(key) is not None:
            return str(account[key])
    return ""


def save_connection(db: Session, token_payload: dict) -> MailIntegration:
    refresh_token = token_payload.get("refresh_token")
    access_token = token_payload.get("access_token")
    if not refresh_token or not access_token:
        raise RuntimeError("Zoho gaf geen refresh token terug. Verwijder eerdere toestemming in Zoho en verbind opnieuw.")

    accounts = get_accounts(access_token)
    if not accounts:
        raise RuntimeError("Geen Zoho Mail-account gevonden.")

    desired = (settings.zoho_from_address or "").lower()
    chosen = None
    if desired:
        chosen = next((a for a in accounts if _account_email(a).lower() == desired), None)
    chosen = chosen or accounts[0]

    account_id = _account_id(chosen)
    email = desired or _account_email(chosen)
    if not account_id or not email:
        raise RuntimeError("Zoho-accountgegevens konden niet worden bepaald.")

    row = db.scalar(select(MailIntegration).where(MailIntegration.provider == PROVIDER))
    if not row:
        row = MailIntegration(
            provider=PROVIDER,
            account_id=account_id,
            email_address=email,
            refresh_token_encrypted=_encrypt(refresh_token),
        )
        db.add(row)
    else:
        row.account_id = account_id
        row.email_address = email
        row.refresh_token_encrypted = _encrypt(refresh_token)
    db.commit()
    db.refresh(row)
    return row


def disconnect(db: Session) -> None:
    row = db.scalar(select(MailIntegration).where(MailIntegration.provider == PROVIDER))
    if row:
        db.delete(row)
        db.commit()


def _to_html_email(content: str) -> str:
    clean = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n", clean) if b.strip()]
    html_blocks = []
    for block in blocks:
        safe = html.escape(block).replace("\n", "<br>")
        html_blocks.append(
            f'<p style="margin:0 0 16px 0;font-family:Arial,Helvetica,sans-serif;'
            f'font-size:15px;line-height:1.6;color:#1f1f1f;">{safe}</p>'
        )
    return '<div style="max-width:640px;">' + "".join(html_blocks) + "</div>"


def send_email(db: Session, to_address: str, subject: str, content: str) -> dict:
    row = db.scalar(select(MailIntegration).where(MailIntegration.provider == PROVIDER))
    if not row:
        raise RuntimeError("Zoho Mail is niet verbonden.")
    access_token = refresh_access_token(_decrypt(row.refresh_token_encrypted))
    body = {
        "fromAddress": row.email_address,
        "toAddress": to_address,
        "subject": subject,
        "content": _to_html_email(content),
        "mailFormat": "html",
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{settings.zoho_mail_base}/api/accounts/{row.account_id}/messages",
            headers=_headers(access_token),
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    status = payload.get("status") or {}
    if status.get("code") not in (200, 201):
        raise RuntimeError(f"Zoho Mail fout: {status.get('description') or payload}")
    return payload
