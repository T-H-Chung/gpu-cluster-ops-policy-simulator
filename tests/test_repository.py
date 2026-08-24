from datetime import datetime, timedelta, timezone

from app.repository import SQLiteRepository


def recommendation() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": "rec-1",
        "policy": "test",
        "policy_version": "version-1",
        "status": "pending_approval",
        "snapshot_hash": "snapshot-1",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }


def test_action_claim_is_idempotent(tmp_path):
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.sync_recommendations([recommendation()])

    first = repository.claim_action(
        recommendation_id="rec-1",
        idempotency_key="key-1",
        actor="tester",
        request_id="request-1",
        reason="approved for test",
    )
    repository.complete_action(
        recommendation_id="rec-1",
        idempotency_key="key-1",
        result={"status": "executed"},
    )
    second = repository.claim_action(
        recommendation_id="rec-1",
        idempotency_key="key-1",
        actor="tester",
        request_id="request-2",
        reason="duplicate",
    )

    assert first["claimed"] is True
    assert second == {
        "claimed": False,
        "duplicate": True,
        "status": "executed",
        "result": {"status": "executed"},
    }


def test_audit_log_is_paginated_and_durable(tmp_path):
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.append_audit("one", {}, "tester", "request-1")
    repository.append_audit("two", {"result": "ok"}, "tester", "request-2")

    page = repository.list_audit(limit=1)
    next_page = repository.list_audit(limit=1, before_id=page[0]["id"])

    assert page[0]["action"] == "two"
    assert next_page[0]["action"] == "one"


def test_in_progress_action_requires_manual_review_after_restart(tmp_path):
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.sync_recommendations([recommendation()])
    repository.claim_action(
        recommendation_id="rec-1",
        idempotency_key="key-1",
        actor="tester",
        request_id="request-1",
        reason="approved for test",
    )

    recovered = repository.recover_interrupted_actions()

    assert recovered == ["rec-1"]
    assert repository.get_action("key-1")["status"] == "interrupted"
    assert repository.get_recommendation("rec-1")["status"] == "manual_review_required"
