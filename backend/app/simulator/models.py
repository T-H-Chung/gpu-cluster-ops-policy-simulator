from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import time


@dataclass
class GPUProfile:
    model: str = "Simulated-Hopper"
    framebuffer_total_mib: int = 81920
    power_limit_default_watts: float = 700.0
    power_limit_min_watts: float = 200.0
    power_limit_max_watts: float = 700.0
    slowdown_temperature_c: float = 87.0
    shutdown_temperature_c: float = 92.0
    idle_power_watts: float = 65.0


@dataclass
class GPU:
    index: int
    uuid: str
    node_name: str
    utilization_pct: float = 2.0
    memory_copy_util_pct: float = 1.0
    memory_used_mib: int = 0
    temperature_c: float = 38.0
    memory_temperature_c: float = 40.0
    power_usage_watts: float = 65.0
    power_limit_watts: float = 700.0
    total_energy_mj: float = 0.0
    sm_clock_mhz: float = 210.0
    xid_errors: int = 0
    ecc_sbe_agg_total: int = 0
    ecc_dbe_agg_total: int = 0
    throttle_reason: str = "none"
    allocated_to_workload_id: int | None = None
    updated_at: float = field(default_factory=time)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Node:
    name: str
    rack_id: str
    ready: bool = True
    unschedulable: bool = False
    cpus_total: int = 64
    cpus_allocated: int = 0
    real_memory_mib: int = 524288
    alloc_memory_mib: int = 0
    taints: list[dict] = field(default_factory=list)
    gpus: list[GPU] = field(default_factory=list)

    def allocated_gpu_count(self) -> int:
        return sum(1 for gpu in self.gpus if gpu.allocated_to_workload_id is not None)

    def free_gpu_count(self) -> int:
        if not self.ready or self.unschedulable:
            return 0
        return len(self.gpus) - self.allocated_gpu_count()

    def max_gpu_temperature_c(self) -> float:
        return max(gpu.temperature_c for gpu in self.gpus)

    def to_dict(self) -> dict:
        return {
            "metadata": {
                "name": self.name,
                "labels": {
                    "node-role.kubernetes.io/gpu": "true",
                    "topology.kubernetes.io/rack": self.rack_id,
                },
            },
            "spec": {
                "unschedulable": self.unschedulable,
                "taints": self.taints,
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True" if self.ready else "False"}],
                "capacity": {
                    "cpu": self.cpus_total,
                    "memory": f"{self.real_memory_mib}Mi",
                    "nvidia.com/gpu": len(self.gpus),
                },
                "allocatable": {
                    "cpu": max(0, self.cpus_total - self.cpus_allocated),
                    "memory": f"{max(0, self.real_memory_mib - self.alloc_memory_mib)}Mi",
                    "nvidia.com/gpu": self.free_gpu_count(),
                },
                "allocated": {
                    "cpu": self.cpus_allocated,
                    "memoryMi": self.alloc_memory_mib,
                    "nvidia.com/gpu": self.allocated_gpu_count(),
                },
            },
            "node_name": self.name,
            "rack_id": self.rack_id,
            "gpus": [gpu.to_dict() for gpu in self.gpus],
            "derived": {
                "free_gpus": self.free_gpu_count(),
                "max_gpu_temperature_celsius": self.max_gpu_temperature_c(),
            },
        }


@dataclass
class Rack:
    rack_id: str
    power_capacity_watts: float = 30000.0
    cooling_capacity_watts: float = 13000.0
    cooling_efficiency: float = 0.85
    inlet_temperature_celsius: float = 24.0
    exhaust_temperature_celsius: float = 35.0
    fan_speed_percent: float = 45.0
    energy_consumed_joules: float = 0.0
    health: str = "OK"

    def to_dict(
        self,
        power_consumed_watts: float,
        power_components: dict[str, float] | None = None,
    ) -> dict:
        effective_cooling = self.cooling_capacity_watts * self.cooling_efficiency
        cooling_headroom = ((effective_cooling - power_consumed_watts) / effective_cooling) * 100
        return {
            "id": self.rack_id,
            "environment_metrics": {
                "power_watts": {"reading": power_consumed_watts},
                "energy_joules": {"reading": self.energy_consumed_joules},
                "inlet_temperature_celsius": {"reading": self.inlet_temperature_celsius},
                "exhaust_temperature_celsius": {"reading": self.exhaust_temperature_celsius},
                "fan_speed_percent": {"reading": self.fan_speed_percent},
            },
            "power_components_watts": power_components or {
                "facility_total": power_consumed_watts,
            },
            "power_capacity_watts": self.power_capacity_watts,
            "cooling_capacity_watts": self.cooling_capacity_watts,
            "cooling_efficiency": self.cooling_efficiency,
            "health": self.health,
            "derived": {
                "cooling_headroom_pct": cooling_headroom,
                "power_headroom_watts": self.power_capacity_watts - power_consumed_watts,
            },
        }


@dataclass
class Job:
    job_id: int
    job_name: str
    namespace: str
    owner_kind: str
    owner_name: str
    priority_class_name: str
    state: str
    reason: str | None
    priority: int
    requested_gpus: int
    duration_seconds: int
    container_name: str = "main"
    elapsed_seconds: int = 0
    node_name: str | None = None
    sla_class: str = "batch"
    checkpointable: bool = True
    checkpoint_interval_seconds: int = 300
    preemptible: bool = True
    flex_class: str = "flex_2"
    max_throughput_reduction_pct: float = 50.0
    deadline_slack_seconds: int = 3600
    geo_shiftable: bool = False
    progress_pct: float = 0.0
    allocated_gpu_uuids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kubernetes": {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "uid": f"pod-{self.job_id}",
                    "name": self.job_name,
                    "namespace": self.namespace,
                    "labels": {
                        "app.kubernetes.io/part-of": self.owner_name,
                        "simulator.gpu-ops/workload-id": str(self.job_id),
                    },
                    "annotations": {
                        "policy.gpu-ops/sla-class": self.sla_class,
                        "policy.gpu-ops/checkpointable": str(self.checkpointable).lower(),
                        "policy.gpu-ops/checkpoint-interval-seconds": str(self.checkpoint_interval_seconds),
                        "policy.gpu-ops/preemptible": str(self.preemptible).lower(),
                        "policy.gpu-ops/flex-class": self.flex_class,
                        "policy.gpu-ops/max-throughput-reduction-pct": str(
                            self.max_throughput_reduction_pct
                        ),
                        "policy.gpu-ops/deadline-slack-seconds": str(
                            self.deadline_slack_seconds
                        ),
                        "policy.gpu-ops/geo-shiftable": str(self.geo_shiftable).lower(),
                    },
                    "ownerReferences": [{"kind": self.owner_kind, "name": self.owner_name}],
                },
                "spec": {
                    "nodeName": self.node_name,
                    "priority": self.priority,
                    "priorityClassName": self.priority_class_name,
                    "containers": [
                        {
                            "name": self.container_name,
                            "resources": {
                                "requests": {"nvidia.com/gpu": self.requested_gpus},
                                "limits": {"nvidia.com/gpu": self.requested_gpus},
                            },
                        }
                    ],
                },
                "status": {
                    "phase": self.state,
                    "reason": self.reason,
                    "elapsedSeconds": self.elapsed_seconds,
                },
            },
            "policy_metadata": {
                "sla_class": self.sla_class,
                "checkpointable": self.checkpointable,
                "checkpoint_interval_seconds": self.checkpoint_interval_seconds,
                "preemptible": self.preemptible,
                "flex_class": self.flex_class,
                "max_throughput_reduction_pct": self.max_throughput_reduction_pct,
                "deadline_slack_seconds": self.deadline_slack_seconds,
                "geo_shiftable": self.geo_shiftable,
            },
            "progress_pct": self.progress_pct,
            "requested_gpus": self.requested_gpus,
            "allocated_gpu_uuids": list(self.allocated_gpu_uuids),
        }


@dataclass
class GridEvent:
    event_id: str
    source: str
    event_type: str
    rack_id: str
    requested_reduction_watts: float
    response_deadline_seconds: int
    duration_seconds: int
    recovery_ramp_watts_per_second: float
    status: str
    created_at: str
    baseline_power_watts: float
    target_power_watts: float
    available_reduction_watts: float
    achieved_reduction_watts: float = 0.0
    compliance_ratio: float = 0.0
    response_time_seconds: float | None = None
    response_deadline_met: bool | None = None
    activated_at: str | None = None
    activated_at_epoch: float | None = None
    recovery_started_at: str | None = None
    closed_at: str | None = None
    failure_reason: str | None = None
    actions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
