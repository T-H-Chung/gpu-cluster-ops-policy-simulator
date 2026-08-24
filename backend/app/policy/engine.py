from __future__ import annotations

import hashlib
import json
import operator
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, time
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    operator: Literal[
        "less_than",
        "less_than_or_equal",
        "greater_than",
        "greater_than_or_equal",
        "equal",
        "not_equal",
    ]
    value: Any
    duration: str | int | float = 0

    @property
    def duration_seconds(self) -> float:
        if isinstance(self.duration, (int, float)):
            if self.duration < 0:
                raise ValueError("duration cannot be negative")
            return float(self.duration)
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", self.duration)
        if not match:
            raise ValueError(f"invalid duration: {self.duration}")
        value, unit = match.groups()
        multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
        return float(value) * multipliers[unit]


class ConditionGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all: list[Condition] = Field(min_length=1)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True


class Guardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_nodes: int = Field(default=1, ge=1)
    cooldown_seconds: int = Field(default=60, ge=0)
    recommendation_ttl_seconds: int = Field(default=300, ge=5)
    max_telemetry_age_seconds: int = Field(default=15, ge=1)
    require_full_node_evacuation: bool = True


class PolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    severity: Literal["info", "warning", "critical"] = "warning"
    when: ConditionGroup
    constraints: ConditionGroup | None = None
    recommend: list[str] = Field(min_length=1)
    approval: Approval = Field(default_factory=Approval)
    guardrails: Guardrails = Field(default_factory=Guardrails)

    @model_validator(mode="after")
    def validate_supported_fields(self) -> "PolicyDocument":
        supported_when = {
            "rack.cooling_headroom_pct",
            "rack.power_headroom_watts",
            "node.max_gpu_temperature_celsius",
            "node.telemetry_age_seconds",
        }
        supported_constraints = {
            "pod.annotation.policy.gpu-ops/sla-class",
            "pod.annotation.policy.gpu-ops/checkpointable",
            "pod.annotation.policy.gpu-ops/preemptible",
        }
        unknown_when = {item.field for item in self.when.all} - supported_when
        unknown_constraints = {
            item.field for item in (self.constraints.all if self.constraints else [])
        } - supported_constraints
        if unknown_when:
            raise ValueError(f"unsupported policy fields: {sorted(unknown_when)}")
        if unknown_constraints:
            raise ValueError(f"unsupported constraint fields: {sorted(unknown_constraints)}")
        for condition in self.when.all:
            condition.duration_seconds
        return self


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "less_than": operator.lt,
    "less_than_or_equal": operator.le,
    "greater_than": operator.gt,
    "greater_than_or_equal": operator.ge,
    "equal": operator.eq,
    "not_equal": operator.ne,
}


class PolicyEngine:
    def __init__(
        self,
        policy: dict[str, Any] | PolicyDocument,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.document = (
            policy if isinstance(policy, PolicyDocument) else PolicyDocument.model_validate(policy)
        )
        self.policy = self.document.model_dump()
        canonical = json.dumps(self.policy, sort_keys=True, separators=(",", ":"))
        self.policy_version = hashlib.sha256(canonical.encode()).hexdigest()
        self._clock = clock
        self._condition_since: dict[tuple[str, str, int], float] = {}
        self._cooldown_until = 0.0

    @classmethod
    def from_yaml(
        cls,
        path: Path,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> "PolicyEngine":
        import yaml

        with path.open("r", encoding="utf-8") as file:
            payload = yaml.safe_load(file)
        return cls(payload, clock=clock)

    def evaluate(self, snapshot: dict) -> list[dict]:
        if self._clock() < self._cooldown_until:
            return []

        actionable: list[dict] = []
        blocked: list[dict] = []
        for rack in snapshot["racks"].values():
            for node in snapshot["nodes"].values():
                if node["rack_id"] != rack["id"]:
                    continue
                scope = (rack["id"], node["metadata"]["name"])
                if not self._conditions_satisfied(rack, node, scope):
                    continue
                job = self._select_eligible_job(snapshot, node["metadata"]["name"])
                if job is None:
                    blocked.append(self._blocked_recommendation(rack, node, snapshot))
                else:
                    actionable.append(self._recommendation(rack, node, job, snapshot))
        candidates = actionable if actionable else blocked
        return candidates[: self.document.guardrails.max_nodes]

    def start_cooldown(self) -> None:
        self._cooldown_until = self._clock() + self.document.guardrails.cooldown_seconds

    def _conditions_satisfied(
        self,
        rack: dict,
        node: dict,
        scope: tuple[str, str],
    ) -> bool:
        now = self._clock()
        all_satisfied = True
        for index, condition in enumerate(self.document.when.all):
            key = (*scope, index)
            actual = self._resolve_when_field(condition.field, rack, node)
            is_true = OPERATORS[condition.operator](actual, condition.value)
            if not is_true:
                self._condition_since.pop(key, None)
                all_satisfied = False
                continue
            since = self._condition_since.setdefault(key, now)
            if now - since < condition.duration_seconds:
                all_satisfied = False
        return all_satisfied

    @staticmethod
    def _resolve_when_field(field: str, rack: dict, node: dict) -> Any:
        values = {
            "rack.cooling_headroom_pct": rack["derived"]["cooling_headroom_pct"],
            "rack.power_headroom_watts": rack["derived"]["power_headroom_watts"],
            "node.max_gpu_temperature_celsius": node["derived"]["max_gpu_temperature_celsius"],
            "node.telemetry_age_seconds": max(
                (max(0.0, time() - gpu["updated_at"]) for gpu in node["gpus"]),
                default=float("inf"),
            ),
        }
        return values[field]

    def _select_eligible_job(self, snapshot: dict, node_name: str) -> dict | None:
        running = [
            job
            for job in snapshot["jobs"].values()
            if job["kubernetes"]["status"]["phase"] == "Running"
            and job["kubernetes"]["spec"]["nodeName"] == node_name
        ]
        eligible = [
            job
            for job in running
            if self._constraints_satisfied(job)
            and self._telemetry_is_fresh(snapshot["nodes"][node_name])
        ]
        if self.document.guardrails.require_full_node_evacuation and (
            len(running) != 1 or len(eligible) != len(running)
        ):
            return None
        if not eligible:
            return None
        return min(eligible, key=lambda item: item["kubernetes"]["spec"]["priority"])

    def _constraints_satisfied(self, job: dict) -> bool:
        if not self.document.constraints:
            return True
        metadata = job["policy_metadata"]
        values = {
            "pod.annotation.policy.gpu-ops/sla-class": metadata["sla_class"],
            "pod.annotation.policy.gpu-ops/checkpointable": metadata["checkpointable"],
            "pod.annotation.policy.gpu-ops/preemptible": metadata["preemptible"],
        }
        return all(
            OPERATORS[condition.operator](values[condition.field], condition.value)
            for condition in self.document.constraints.all
        )

    def _telemetry_is_fresh(self, node: dict) -> bool:
        max_age = self.document.guardrails.max_telemetry_age_seconds
        return all(time() - gpu["updated_at"] <= max_age for gpu in node["gpus"])

    def _recommendation(self, rack: dict, node: dict, job: dict, snapshot: dict) -> dict:
        job_id = int(job["kubernetes"]["metadata"]["labels"]["simulator.gpu-ops/workload-id"])
        pod_name = job["kubernetes"]["metadata"]["name"]
        namespace = job["kubernetes"]["metadata"]["namespace"]
        node_name = node["metadata"]["name"]
        recommendation_id = (
            f"rec-{self.policy_version[:8]}-{job_id}-{node_name}"
        )
        return {
            "id": recommendation_id,
            "policy": self.document.name,
            "policy_version": self.policy_version,
            "severity": self.document.severity,
            "status": "pending_approval",
            "summary": f"Cordon {node_name}, evict {namespace}/{pod_name}, and requeue workload-{job_id}",
            "target": {
                "node": node_name,
                "job_id": job_id,
                "pod": pod_name,
                "namespace": namespace,
                "rack": rack["id"],
            },
            "evidence": self._evidence(rack, node, namespace, pod_name),
            "impact": {
                "affected_pods": 1,
                "temporarily_removed_gpus": len(node["gpus"]),
                "estimated_extra_runtime_seconds": "20-40",
            },
            "recommended_actions": list(self.document.recommend),
            "approval_required": self.document.approval.required,
            "snapshot_hash": self._snapshot_hash(snapshot),
            **self._timestamps(),
        }

    def _blocked_recommendation(self, rack: dict, node: dict, snapshot: dict) -> dict:
        node_name = node["metadata"]["name"]
        return {
            "id": f"blocked-{self.policy_version[:8]}-{node_name}",
            "policy": self.document.name,
            "policy_version": self.policy_version,
            "severity": self.document.severity,
            "status": "blocked_by_guardrail",
            "summary": f"Automatic eviction blocked for {node_name}",
            "target": {"node": node_name, "rack": rack["id"]},
            "evidence": [
                *self._evidence(rack, node),
                "the node cannot be fully evacuated within policy constraints and telemetry freshness guardrails",
            ],
            "recommended_actions": ["manual_intervention"],
            "approval_required": False,
            "snapshot_hash": self._snapshot_hash(snapshot),
            **self._timestamps(),
        }

    @staticmethod
    def _evidence(
        rack: dict,
        node: dict,
        namespace: str | None = None,
        pod_name: str | None = None,
    ) -> list[str]:
        evidence = [
            f"rack {rack['id']} cooling headroom is {rack['derived']['cooling_headroom_pct']:.1f}%",
            f"{node['metadata']['name']} max GPU temperature is {node['derived']['max_gpu_temperature_celsius']:.1f} C",
        ]
        if namespace and pod_name:
            evidence.append(f"{namespace}/{pod_name} satisfies policy constraints")
        return evidence

    def _timestamps(self) -> dict[str, str]:
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(
            seconds=self.document.guardrails.recommendation_ttl_seconds
        )
        return {
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    @staticmethod
    def _snapshot_hash(snapshot: dict) -> str:
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
