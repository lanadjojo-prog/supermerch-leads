import base64
import hmac

from fastapi import HTTPException, Request, status

from .config import settings


def require_basic_auth(request: Request) -> None:
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})

    if not (
        hmac.compare_digest(username, settings.app_username)
        and hmac.compare_digest(password, settings.app_password)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
