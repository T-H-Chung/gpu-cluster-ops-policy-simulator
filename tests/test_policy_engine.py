from time import time

import pytest
from pydantic import ValidationError

from app.policy.engine import PolicyEngine


def policy(*, duration: str = "0s") -> dict:
    return {
        "name": "rack-thermal-protection",
        "severity": "critical",
        "when": {
            "all": [
                {
                    "field": "rack.cooling_headroom_pct",
                    "operator": "less_than",
                    "value": 10,
                    "duration": duration,
                },
                {
                    "field": "node.max_gpu_temperature_celsius",
                    "operator": "greater_than",
                    "value": 84,
                    "duration": duration,
                },
            ]
        },
        "constraints": {
            "all": [
                {
                    "field": "pod.annotation.policy.gpu-ops/sla-class",
                    "operator": "not_equal",
                    "value": "production",
                },
                {
                    "field": "pod.annotation.policy.gpu-ops/checkpointable",
                    "operator": "equal",
                    "value": True,
                },
                {
                    "field": "pod.annotation.policy.gpu-ops/preemptible",
                    "operator": "equal",
                    "value": True,
                },
            ]
        },
        "recommend": [
            "checkpoint_workload",
            "cordon_node",
            "evict_pod",
            "requeue_workload",
        ],
        "approval": {"required": True},
        "guardrails": {
            "cooldown_seconds": 0,
            "recommendation_ttl_seconds": 300,
            "max_telemetry_age_seconds": 15,
            "require_full_node_evacuation": True,
        },
    }


def hot_snapshot(*, sla_class: str = "batch", checkpointable: bool = True) -> dict:
    return {
        "observed_at": "2026-01-01T00:00:00+00:00",
        "racks": {
            "rack-a": {
                "id": "rack-a",
                "derived": {
                    "cooling_headroom_pct": 3.0,
                    "power_headroom_watts": 1000,
                },
            }
        },
        "nodes": {
            "gpu-node-01": {
                "metadata": {"name": "gpu-node-01"},
                "rack_id": "rack-a",
                "gpus": [{"updated_at": time()}, {"updated_at": time()}],
                "derived": {"max_gpu_temperature_celsius": 86.0},
            }
        },
        "jobs": {
            101: {
                "kubernetes": {
                    "metadata": {
                        "name": "llama-training-pod",
                        "namespace": "ml-workloads",
                        "labels": {"simulator.gpu-ops/workload-id": "101"},
                    },
                    "spec": {"nodeName": "gpu-node-01", "priority": 100},
                    "status": {"phase": "Running"},
                },
                "policy_metadata": {
                    "checkpointable": checkpointable,
                    "preemptible": True,
                    "sla_class": sla_class,
                },
            }
        },
    }


def test_policy_recommends_checkpointable_batch_job():
    engine = PolicyEngine(policy())

    recommendations = engine.evaluate(hot_snapshot())

    assert recommendations[0]["status"] == "pending_approval"
    assert recommendations[0]["target"]["job_id"] == 101
    assert recommendations[0]["recommended_actions"] == policy()["recommend"]


def test_policy_enforces_duration():
    now = [100.0]
    engine = PolicyEngine(policy(duration="20s"), clock=lambda: now[0])

    assert engine.evaluate(hot_snapshot()) == []
    now[0] += 19.9
    assert engine.evaluate(hot_snapshot()) == []
    now[0] += 0.1
    assert engine.evaluate(hot_snapshot())[0]["status"] == "pending_approval"


def test_policy_blocks_production_workload():
    engine = PolicyEngine(policy())

    recommendation = engine.evaluate(hot_snapshot(sla_class="production"))[0]

    assert recommendation["status"] == "blocked_by_guardrail"
    assert recommendation["approval_required"] is False


def test_policy_does_not_partially_evacuate_a_node():
    snapshot = hot_snapshot()
    production = hot_snapshot(sla_class="production")["jobs"][101]
    production["kubernetes"]["metadata"]["labels"][
        "simulator.gpu-ops/workload-id"
    ] = "102"
    production["kubernetes"]["metadata"]["name"] = "production-neighbor"
    snapshot["jobs"][102] = production
    engine = PolicyEngine(policy())

    recommendation = engine.evaluate(snapshot)[0]

    assert recommendation["status"] == "blocked_by_guardrail"


def test_policy_rejects_unknown_fields():
    document = policy()
    document["when"]["all"][0]["field"] = "rack.typo"

    with pytest.raises(ValidationError):
        PolicyEngine(document)
