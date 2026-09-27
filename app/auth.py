import base64
import hmac

from fastapi import HTTPException, Request, status

from .config import settings


def require_basic_auth(request: Request) -> None:
    if not settings.app_password or settings.app_password == "change-me":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APP_PASSWORD is nog niet ingesteld op Render.",
        )

    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic realm=SuperMerch Leads"},
        )
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic realm=SuperMerch Leads"},
        )

    if not (
        hmac.compare_digest(username, settings.app_username)
        and hmac.compare_digest(password, settings.app_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic realm=SuperMerch Leads"},
        )
