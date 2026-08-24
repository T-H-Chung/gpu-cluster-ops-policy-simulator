from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS simulator_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp
                    ON audit_events(timestamp DESC);

                CREATE TABLE IF NOT EXISTS recommendations (
                    id TEXT PRIMARY KEY,
                    policy TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approved_by TEXT,
                    approved_at TEXT,
                    approval_reason TEXT,
                    result_json TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recommendations_status
                    ON recommendations(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS action_executions (
                    idempotency_key TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    result_json TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                """
            )

    def ping(self) -> bool:
        try:
            with self._connect() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def recover_interrupted_actions(self) -> list[str]:
        result = json.dumps(
            {
                "status": "interrupted",
                "reason": "service restarted before durable action completion",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT recommendation_id
                FROM action_executions
                WHERE status = 'executing'
                """
            ).fetchall()
            recommendation_ids = [row["recommendation_id"] for row in rows]
            connection.execute(
                """
                UPDATE action_executions
                SET status = 'interrupted', result_json = ?, completed_at = ?
                WHERE status = 'executing'
                """,
                (result, now),
            )
            connection.execute(
                """
                UPDATE recommendations
                SET status = 'manual_review_required', result_json = ?, updated_at = ?
                WHERE status = 'executing'
                """,
                (result, now),
            )
        return recommendation_ids

    def save_state(self, state: dict) -> None:
        serialized = json.dumps(state, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO simulator_state(id, state_json, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (serialized, utc_now()),
            )

    def load_state(self) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM simulator_state WHERE id = 1"
            ).fetchone()
        return json.loads(row["state_json"]) if row else None

    def append_audit(
        self,
        action: str,
        detail: dict,
        actor: str,
        request_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(timestamp, actor, request_id, action, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    actor,
                    request_id,
                    action,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                ),
            )

    def list_audit(self, *, limit: int, before_id: int | None = None) -> list[dict]:
        query = """
            SELECT id, timestamp, actor, request_id, action, detail_json
            FROM audit_events
        """
        parameters: list[Any] = []
        if before_id is not None:
            query += " WHERE id < ?"
            parameters.append(before_id)
        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "actor": row["actor"],
                "request_id": row["request_id"],
                "action": row["action"],
                "detail": json.loads(row["detail_json"]),
            }
            for row in rows
        ]

    def prune_audit(self, *, retention_days: int, max_rows: int) -> int:
        if retention_days < 1 or max_rows < 1:
            raise ValueError("audit retention limits must be positive")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            old_cursor = connection.execute(
                "DELETE FROM audit_events WHERE timestamp < ?",
                (cutoff,),
            )
            overflow_cursor = connection.execute(
                """
                DELETE FROM audit_events
                WHERE id NOT IN (
                    SELECT id FROM audit_events ORDER BY id DESC LIMIT ?
                )
                """,
                (max_rows,),
            )
        return old_cursor.rowcount + overflow_cursor.rowcount

    def sync_recommendations(self, recommendations: list[dict]) -> None:
        now = utc_now()
        active_ids = {item["id"] for item in recommendations}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in recommendations:
                connection.execute(
                    """
                    INSERT INTO recommendations(
                        id, policy, policy_version, status, payload_json,
                        snapshot_hash, created_at, expires_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status = CASE
                            WHEN recommendations.status IN (
                                'pending_approval', 'blocked_by_guardrail', 'stale'
                            )
                            THEN excluded.status ELSE recommendations.status END,
                        payload_json = CASE
                            WHEN recommendations.status IN (
                                'pending_approval', 'blocked_by_guardrail', 'stale'
                            )
                            THEN excluded.payload_json ELSE recommendations.payload_json END,
                        snapshot_hash = CASE
                            WHEN recommendations.status IN (
                                'pending_approval', 'blocked_by_guardrail', 'stale'
                            )
                            THEN excluded.snapshot_hash ELSE recommendations.snapshot_hash END,
                        expires_at = CASE
                            WHEN recommendations.status IN (
                                'pending_approval', 'blocked_by_guardrail', 'stale'
                            )
                            THEN excluded.expires_at ELSE recommendations.expires_at END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        item["id"],
                        item["policy"],
                        item["policy_version"],
                        item["status"],
                        json.dumps(item, separators=(",", ":"), sort_keys=True),
                        item["snapshot_hash"],
                        item["created_at"],
                        item["expires_at"],
                        now,
                    ),
                )
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                connection.execute(
                    f"""
                    UPDATE recommendations
                    SET status = 'stale', updated_at = ?
                    WHERE status IN ('pending_approval', 'blocked_by_guardrail')
                      AND id NOT IN ({placeholders})
                    """,
                    [now, *active_ids],
                )
            else:
                connection.execute(
                    """
                    UPDATE recommendations
                    SET status = 'stale', updated_at = ?
                    WHERE status IN ('pending_approval', 'blocked_by_guardrail')
                    """,
                    (now,),
                )

    def list_active_recommendations(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, status
                FROM recommendations
                WHERE status IN ('pending_approval', 'blocked_by_guardrail', 'executing')
                ORDER BY created_at DESC
                """
            ).fetchall()
        items = []
        for row in rows:
            item = json.loads(row["payload_json"])
            item["status"] = row["status"]
            items.append(item)
        return items

    def get_recommendation(self, recommendation_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, status, result_json FROM recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
        if row is None:
            return None
        item = json.loads(row["payload_json"])
        item["status"] = row["status"]
        if row["result_json"]:
            item["result"] = json.loads(row["result_json"])
        return item

    def get_action(self, idempotency_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recommendation_id, status, result_json
                FROM action_executions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "recommendation_id": row["recommendation_id"],
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def claim_action(
        self,
        *,
        recommendation_id: str,
        idempotency_key: str,
        actor: str,
        request_id: str,
        reason: str,
    ) -> dict:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT status, result_json
                FROM action_executions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if previous:
                return {
                    "claimed": False,
                    "duplicate": True,
                    "status": previous["status"],
                    "result": json.loads(previous["result_json"])
                    if previous["result_json"]
                    else None,
                }
            recommendation = connection.execute(
                "SELECT status, expires_at FROM recommendations WHERE id = ?",
                (recommendation_id,),
            ).fetchone()
            if recommendation is None:
                return {"claimed": False, "duplicate": False, "reason": "not_found"}
            if recommendation["status"] != "pending_approval":
                return {
                    "claimed": False,
                    "duplicate": False,
                    "reason": f"recommendation_{recommendation['status']}",
                }
            if recommendation["expires_at"] <= now:
                connection.execute(
                    "UPDATE recommendations SET status = 'expired', updated_at = ? WHERE id = ?",
                    (now, recommendation_id),
                )
                return {"claimed": False, "duplicate": False, "reason": "expired"}
            connection.execute(
                """
                INSERT INTO action_executions(
                    idempotency_key, recommendation_id, status,
                    actor, request_id, started_at
                )
                VALUES (?, ?, 'executing', ?, ?, ?)
                """,
                (idempotency_key, recommendation_id, actor, request_id, now),
            )
            connection.execute(
                """
                UPDATE recommendations
                SET status = 'executing', approved_by = ?, approved_at = ?,
                    approval_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (actor, now, reason, now, recommendation_id),
            )
        return {"claimed": True, "duplicate": False}

    def complete_action(
        self,
        *,
        recommendation_id: str,
        idempotency_key: str,
        result: dict,
    ) -> None:
        action_status = result.get("status", "failed")
        recommendation_status = (
            "executed" if action_status == "executed" else "blocked"
            if action_status == "blocked"
            else "failed"
        )
        serialized = json.dumps(result, separators=(",", ":"), sort_keys=True)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE action_executions
                SET status = ?, result_json = ?, completed_at = ?
                WHERE idempotency_key = ?
                """,
                (action_status, serialized, now, idempotency_key),
            )
            connection.execute(
                """
                UPDATE recommendations
                SET status = ?, result_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (recommendation_status, serialized, now, recommendation_id),
            )
