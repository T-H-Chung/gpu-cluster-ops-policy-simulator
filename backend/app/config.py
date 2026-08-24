from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    auth_enabled: bool
    api_keys: dict[str, str]
    simulator_seed: int
    audit_retention_days: int
    audit_max_rows: int

    @classmethod
    def from_env(cls, default_database_path: Path) -> "Settings":
        auth_enabled = os.getenv("GPU_OPS_AUTH_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        raw_keys = os.getenv("GPU_OPS_API_KEYS", "{}")
        try:
            api_keys = json.loads(raw_keys)
        except json.JSONDecodeError as error:
            raise ValueError("GPU_OPS_API_KEYS must be a JSON object") from error
        if not isinstance(api_keys, dict) or any(
            role not in {"viewer", "operator", "admin"}
            or not isinstance(key, str)
            or not key
            for role, key in api_keys.items()
        ):
            raise ValueError("GPU_OPS_API_KEYS must map viewer/operator/admin roles to keys")
        if auth_enabled and not api_keys:
            raise ValueError("authentication is enabled but GPU_OPS_API_KEYS is empty")
        return cls(
            database_path=Path(
                os.getenv("GPU_OPS_DB_PATH", str(default_database_path))
            ).expanduser(),
            auth_enabled=auth_enabled,
            api_keys=api_keys,
            simulator_seed=int(os.getenv("GPU_OPS_SIMULATOR_SEED", "42")),
            audit_retention_days=int(os.getenv("GPU_OPS_AUDIT_RETENTION_DAYS", "90")),
            audit_max_rows=int(os.getenv("GPU_OPS_AUDIT_MAX_ROWS", "100000")),
        )
