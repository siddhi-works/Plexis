"""Alexa+ two-tier OAuth, implemented as a local development authority.

Tier 1 - `client_credentials` (HTTP Basic client auth) issues an `mcp:service`
token, used by the host for `initialize`, `tools/list` and health checks.
Tier 2 - `authorization_code` with PKCE (S256) issues `mcp:tools mcp:resources`
for the customer's own actions (booking, household memory, calls).

Tokens are short-lived JWTs (no refresh tokens) bound to this server's
resource URL (RFC 8707 audience). In production the authorization server is
Login with Amazon / Alexa account linking, or Amazon Bedrock AgentCore
Identity; Plexis only depends on the verifier below.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import jwt
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

SERVICE_SCOPE = "mcp:service"
USER_SCOPES = ["mcp:service", "mcp:tools", "mcp:resources"]
DEV_CLIENTS = {"alexa-plus-simulator": "dev-secret-change-me"}


class Authority:
    def __init__(self, state_dir: Path, issuer: str, resource: str, secret: str = "") -> None:
        self.issuer = issuer.rstrip("/")
        self.resource = resource
        path = state_dir / "keys" / "jwt.secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        if secret:
            self.secret = secret
        elif path.exists():
            self.secret = path.read_text().strip()
        else:
            self.secret = secrets.token_urlsafe(48)
            path.write_text(self.secret)
        self._codes: dict[str, dict[str, Any]] = {}

    # ----------------------------------------------------------- issuing

    def _issue(
        self, client_id: str, scopes: list[str], subject: str | None, ttl: int = 3600
    ) -> dict[str, Any]:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.resource,
            "sub": subject or client_id,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + ttl,
            "jti": secrets.token_hex(8),
        }
        return {
            "access_token": jwt.encode(claims, self.secret, algorithm="HS256"),
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": " ".join(scopes),
        }

    async def token(self, request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        client_id, client_secret = _basic(request) or (form.get("client_id"), form.get("client_secret"))
        if not self._resource_ok(form.get("resource")):
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        if grant == "client_credentials":
            if DEV_CLIENTS.get(str(client_id)) != client_secret:
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            requested = str(form.get("scope") or SERVICE_SCOPE).split()
            if any(s != SERVICE_SCOPE for s in requested):
                return JSONResponse({"error": "invalid_scope"}, status_code=400)
            return JSONResponse(self._issue(str(client_id), [SERVICE_SCOPE], None))
        if grant == "authorization_code":
            code = self._codes.pop(str(form.get("code")), None)
            verifier = str(form.get("code_verifier") or "")
            if code is None or code["exp"] < time.time():
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            )
            if challenge != code["challenge"]:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400,
                )
            return JSONResponse(self._issue(code["client_id"], USER_SCOPES, code["subject"]))
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    def _resource_ok(self, value: object) -> bool:
        """RFC 8707: Alexa+ sends the canonical MCP URI as `resource`; tokens are only minted for this server."""
        return value in (None, "") or str(value).rstrip("/") == self.resource.rstrip("/")

    async def authorize(self, request: Request) -> Response:
        """Development consent screen: auto-approves for the demo household account."""
        q = request.query_params
        if (
            q.get("response_type") != "code"
            or q.get("code_challenge_method") != "S256"
            or not q.get("code_challenge")
        ):
            return JSONResponse(
                {"error": "invalid_request", "error_description": "PKCE S256 is required"}, status_code=400
            )
        if not self._resource_ok(q.get("resource")):
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        code = secrets.token_urlsafe(24)
        self._codes[code] = {
            "challenge": q["code_challenge"],
            "client_id": q.get("client_id", "unknown"),
            "subject": "household-demo",
            "exp": time.time() + 120,
        }
        if q.get("format") == "json" or not q.get("redirect_uri"):
            return JSONResponse({"code": code, "state": q.get("state")})
        return RedirectResponse(
            f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q.get('state', '')})}"
        )

    async def metadata(self, _: Request) -> Response:
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/oauth/authorize",
                "token_endpoint": f"{self.issuer}/oauth/token",
                "grant_types_supported": ["client_credentials", "authorization_code"],
                "response_types_supported": ["code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "scopes_supported": USER_SCOPES,
            }
        )

    # ----------------------------------------------------------- verifying

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token, self.secret, algorithms=["HS256"], audience=self.resource, issuer=self.issuer
            )
        except jwt.PyJWTError:
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", "unknown"),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=claims.get("exp"),
            resource=self.resource,
            subject=claims.get("sub"),
            claims={"iss": claims.get("iss")},
        )


def _basic(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:]).decode()
        cid, _, sec = raw.partition(":")
        return cid, sec
    except Exception:
        return None
