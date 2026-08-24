from pathlib import Path

import yaml

from app.policy.engine import PolicyEngine
from app.repository import SQLiteRepository
from app.services import RecommendationService
from app.simulator.cluster import ClusterSimulator


ROOT = Path(__file__).resolve().parents[1]


def immediate_policy() -> dict:
    with (ROOT / "policies" / "rack-thermal-protection.yaml").open(
        "r",
        encoding="utf-8",
    ) as file:
        policy = yaml.safe_load(file)
    for condition in policy["when"]["all"]:
        condition["duration"] = "0s"
    policy["guardrails"]["cooldown_seconds"] = 0
    return policy


def test_approval_is_revalidated_verified_and_idempotent(tmp_path):
    repository = SQLiteRepository(tmp_path / "service.db")
    simulator = ClusterSimulator(
        seed=42,
        audit_sink=repository.append_audit,
        state_sink=repository.save_state,
    )
    simulator.racks["rack-a"].cooling_efficiency = 0.02
    for node in simulator.nodes.values():
        for gpu in node.gpus:
            gpu.temperature_c = 90
    service = RecommendationService(
        simulator,
        PolicyEngine(immediate_policy()),
        repository,
    )
    recommendation = service.refresh()[0]

    first = service.approve(
        recommendation_id=recommendation["id"],
        idempotency_key="approval-key",
        actor="test-admin",
        request_id="request-1",
        reason="protect the overheated rack",
    )
    duplicate = service.approve(
        recommendation_id=recommendation["id"],
        idempotency_key="approval-key",
        actor="test-admin",
        request_id="request-2",
        reason="duplicate request",
    )

    assert first["status"] == "executed"
    assert first["verification"] == {
        "node_cordoned": True,
        "workload_removed_from_target": True,
        "invariant_errors": [],
    }
    assert duplicate == first
    approval_events = [
        item
        for item in repository.list_audit(limit=100)
        if item["action"] == "approve_recommendation"
    ]
    assert len(approval_events) == 1
