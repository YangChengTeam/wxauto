from __future__ import annotations

import secrets

from mcp.server.auth.provider import AccessToken, TokenVerifier


class StaticTokenVerifier(TokenVerifier):
    """Minimal bearer-token verifier for a private Windows deployment."""

    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="wxclient-mcp-agent",
            scopes=["wxclient:control"],
        )
