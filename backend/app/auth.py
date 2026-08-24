from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable, Literal

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings


Role = Literal["viewer", "operator", "admin"]
ROLE_LEVEL = {"viewer": 1, "operator": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    actor: str
    role: Role


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bearer = HTTPBearer(auto_error=False)

    def require(self, minimum_role: Role) -> Callable:
        async def dependency(
            credentials: HTTPAuthorizationCredentials | None = Depends(self.bearer),
        ) -> Principal:
            if not self.settings.auth_enabled:
                return Principal(actor="local-development", role="admin")
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="missing bearer API key",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            matched_role: Role | None = None
            for role, expected_key in self.settings.api_keys.items():
                if secrets.compare_digest(credentials.credentials, expected_key):
                    matched_role = role  # type: ignore[assignment]
                    break
            if matched_role is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="invalid API key",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if ROLE_LEVEL[matched_role] < ROLE_LEVEL[minimum_role]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"{minimum_role} role required",
                )
            return Principal(actor=f"api-key:{matched_role}", role=matched_role)

        return dependency
