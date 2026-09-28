from __future__ import annotations

from functools import lru_cache

import jwt
from jwt import PyJWKClient


GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_AUDIENCE = "supermerch-lead-engine"
GITHUB_REPOSITORY = "lanadjojo-prog/supermerch-leads"
GITHUB_WORKFLOW_REF = (
    "lanadjojo-prog/supermerch-leads/.github/workflows/"
    "lead-engine-keepalive.yml@refs/heads/main"
)


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(f"{GITHUB_OIDC_ISSUER}/.well-known/jwks")


def verify_github_actions_oidc(authorization_header: str | None) -> dict:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise ValueError("Missing GitHub Actions OIDC bearer token.")

    token = authorization_header.removeprefix("Bearer ").strip()
    if not token:
        raise ValueError("Missing GitHub Actions OIDC bearer token.")

    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=GITHUB_OIDC_AUDIENCE,
        issuer=GITHUB_OIDC_ISSUER,
        leeway=30,
    )

    if claims.get("repository") != GITHUB_REPOSITORY:
        raise ValueError("Unexpected GitHub repository.")
    if claims.get("ref") != "refs/heads/main":
        raise ValueError("Unexpected GitHub ref.")
    if claims.get("workflow_ref") != GITHUB_WORKFLOW_REF:
        raise ValueError("Unexpected GitHub workflow.")
    if claims.get("event_name") not in {"schedule", "workflow_dispatch"}:
        raise ValueError("Unexpected GitHub Actions event.")

    return claims
