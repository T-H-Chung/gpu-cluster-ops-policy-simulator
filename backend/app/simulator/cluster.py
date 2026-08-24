from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from time import sleep, time
from typing import Any, Callable
from uuid import uuid4

from app.grid.control import ActionArbiter
from app.simulator.models import GPU, GPUProfile, GridEvent, Job, Node, Rack


class ClusterSimulator:
    STATE_VERSION = 1

    def __init__(
        self,
        seed: int = 42,
        *,
        audit_sink: Callable[[str, dict, str, str], None] | None = None,
        state_sink: Callable[[dict], None] | None = None,
    ) -> None:
        self.random = random.Random(seed)
        self.seed = seed
        self.profile = GPUProfile()
        self.racks = {"rack-a": Rack(rack_id="rack-a")}
        self.nodes = self._create_nodes()
        self.jobs = self._create_seed_jobs()
        self.grid_events: dict[str, GridEvent] = {}
        self.action_arbiter = ActionArbiter(
            minimum_gpu_power_limit_watts=self.profile.power_limit_min_watts,
            thermal_slowdown_temperature_c=self.profile.slowdown_temperature_c,
        )
        self._recent_audit: deque[dict[str, Any]] = deque(maxlen=1000)
        self._audit_sink: Callable[[str, dict, str, str], None] | None = None
        self._state_sink: Callable[[dict], None] | None = None
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_tick = time()
        self._schedule_pending_jobs()
        self._audit_sink = audit_sink
        self._state_sink = state_sink

    def _create_nodes(self) -> dict[str, Node]:
        nodes = {}
        for node_idx in range(1, 5):
            node_name = f"gpu-node-{node_idx:02d}"
            gpus = [
                GPU(
                    index=gpu_idx,
                    uuid=f"GPU-{node_idx:02d}{gpu_idx:02d}",
                    node_name=node_name,
                    power_limit_watts=self.profile.power_limit_default_watts,
                )
                for gpu_idx in range(2)
            ]
            nodes[node_name] = Node(name=node_name, rack_id="rack-a", gpus=gpus)
        return nodes

    def _create_seed_jobs(self) -> dict[int, Job]:
        jobs = {}
        specs = [
            ("llama-training-pod", 2, 900, 900, "batch-normal", "batch", True, "Job", "llama-training"),
            ("embedding-batch-pod", 1, 500, 600, "batch-normal", "batch", True, "Job", "embedding-batch"),
            ("vision-finetune-pod", 2, 700, 850, "batch-normal", "batch", True, "Job", "vision-finetune"),
            ("inference-prod-pod", 1, 1200, 2000, "production-high", "production", False, "Deployment", "inference-prod"),
            ("research-sweep-pod", 1, 420, 500, "batch-normal", "batch", True, "Job", "research-sweep"),
            ("eval-run-pod", 1, 300, 400, "batch-low", "batch", True, "Job", "eval-run"),
        ]
        for offset, spec in enumerate(specs, start=101):
            name, gpus, duration, priority, priority_class, sla, checkpointable, owner_kind, owner_name = spec
            jobs[offset] = Job(
                job_id=offset,
                job_name=name,
                namespace="ml-workloads",
                owner_kind=owner_kind,
                owner_name=owner_name,
                priority_class_name=priority_class,
                state="Pending",
                reason="Resources",
                priority=priority,
                requested_gpus=gpus,
                duration_seconds=duration,
                sla_class=sla,
                checkpointable=checkpointable,
                preemptible=checkpointable,
                flex_class="flex_0" if sla == "production" else "flex_2",
            )
        return jobs

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def audit_log(self) -> list[dict[str, Any]]:
        """Bounded compatibility view; the durable repository is authoritative."""
        with self._lock:
            return list(self._recent_audit)

    def _run_loop(self) -> None:
        while self._running:
            self.tick()
            sleep(1)

    def tick(self) -> None:
        with self._lock:
            now = time()
            elapsed = max(now - self._last_tick, 0.1)
            self._last_tick = now
            self._update_jobs(elapsed)
            self._schedule_pending_jobs()
            self._advance_grid_events(elapsed, now)
            self._update_gpus(elapsed)
            self._update_racks(elapsed)
            self._update_grid_event_tracking()
            self._persist_state()

    def _update_jobs(self, elapsed: float) -> None:
        for job in self.jobs.values():
            if job.state != "Running":
                continue
            slowdown = self._job_slowdown_ratio(job)
            job.elapsed_seconds += int(elapsed)
            job.progress_pct = min(
                100.0,
                job.progress_pct + (elapsed / job.duration_seconds) * 100.0 / slowdown,
            )
            if job.progress_pct >= 100.0:
                self._release_job(job)
                job.state = "Succeeded"
                job.reason = None

    def _schedule_pending_jobs(self) -> None:
        pending = sorted(
            [job for job in self.jobs.values() if job.state == "Pending"],
            key=lambda item: item.priority,
            reverse=True,
        )
        for job in pending:
            node = next(
                (candidate for candidate in self.nodes.values() if candidate.free_gpu_count() >= job.requested_gpus),
                None,
            )
            if node is None:
                continue
            job.state = "Running"
            job.reason = None
            job.node_name = node.name
            node.cpus_allocated += 16 * job.requested_gpus
            node.alloc_memory_mib += 65536 * job.requested_gpus
            allocated = 0
            for gpu in node.gpus:
                if gpu.allocated_to_workload_id is None and allocated < job.requested_gpus:
                    gpu.allocated_to_workload_id = job.job_id
                    gpu.memory_used_mib = int(self.profile.framebuffer_total_mib * self.random.uniform(0.55, 0.90))
                    gpu.utilization_pct = 90.0
                    gpu.power_usage_watts = min(gpu.power_limit_watts, 600.0)
                    gpu.sm_clock_mhz = 1700.0
                    job.allocated_gpu_uuids.append(gpu.uuid)
                    allocated += 1
            self._audit("schedule_pod", {"workload_id": job.job_id, "pod": job.job_name, "node": node.name})

    def _update_racks(self, elapsed: float) -> None:
        for rack in self.racks.values():
            components = self._rack_power_components(rack.rack_id)
            facility_power = components["facility_total"]
            rack.energy_consumed_joules += facility_power * elapsed
            heat_ratio = components["it_total"] / max(
                rack.cooling_capacity_watts * rack.cooling_efficiency,
                1,
            )
            target_inlet = 23.5 + max(0.0, heat_ratio - 0.55) * 18.0
            rack.inlet_temperature_celsius += (target_inlet - rack.inlet_temperature_celsius) * 0.08
            rack.exhaust_temperature_celsius = rack.inlet_temperature_celsius + 9.0 + heat_ratio * 5.0
            rack.fan_speed_percent = min(100.0, 35.0 + heat_ratio * 55.0)
            rack.health = "WARNING" if heat_ratio > 0.90 else "OK"

    def _update_gpus(self, elapsed: float) -> None:
        for node in self.nodes.values():
            rack = self.racks[node.rack_id]
            for gpu in node.gpus:
                busy = gpu.memory_used_mib > 0
                gpu.utilization_pct = self.random.uniform(78, 98) if busy else self.random.uniform(0, 5)
                gpu.memory_copy_util_pct = self.random.uniform(25, 70) if busy else self.random.uniform(0, 3)
                base_power = 65 if not busy else 250 + gpu.utilization_pct * 4.2
                gpu.power_usage_watts = min(gpu.power_limit_watts, base_power + self.random.uniform(-18, 18))
                thermal_load = (
                    gpu.power_usage_watts
                    / self.profile.power_limit_max_watts
                ) * 24.0
                target_temp = rack.inlet_temperature_celsius + thermal_load + (8.0 if busy else 2.0)
                gpu.temperature_c += (target_temp - gpu.temperature_c) * 0.10
                gpu.temperature_c += self.random.uniform(-0.25, 0.25)
                gpu.memory_temperature_c = gpu.temperature_c + self.random.uniform(2.0, 6.0)
                gpu.total_energy_mj += gpu.power_usage_watts * elapsed * 1000
                if gpu.temperature_c >= self.profile.slowdown_temperature_c:
                    gpu.throttle_reason = "thermal_slowdown"
                    gpu.sm_clock_mhz = self.random.uniform(900, 1250)
                elif gpu.power_usage_watts >= gpu.power_limit_watts * 0.97:
                    gpu.throttle_reason = "power_limit"
                    gpu.sm_clock_mhz = self.random.uniform(1300, 1550)
                else:
                    gpu.throttle_reason = "none"
                    gpu.sm_clock_mhz = self.random.uniform(1600, 1800) if busy else self.random.uniform(200, 400)
                gpu.updated_at = time()

    def _job_slowdown_ratio(self, job: Job) -> float:
        if not job.node_name:
            return 1.0
        node = self.nodes[job.node_name]
        throttled = any(gpu.throttle_reason != "none" for gpu in node.gpus)
        return 1.25 if throttled else 1.0

    def _release_job(self, job: Job) -> None:
        if job.node_name:
            node = self.nodes[job.node_name]
            for gpu in node.gpus:
                if gpu.allocated_to_workload_id == job.job_id:
                    gpu.allocated_to_workload_id = None
                    gpu.memory_used_mib = 0
            node.cpus_allocated = max(0, node.cpus_allocated - 16 * job.requested_gpus)
            node.alloc_memory_mib = max(0, node.alloc_memory_mib - 65536 * job.requested_gpus)
        job.node_name = None
        job.allocated_gpu_uuids = []

    def _rack_power(self, rack_id: str) -> float:
        return self._rack_power_components(rack_id)["facility_total"]

    def _rack_power_components(self, rack_id: str) -> dict[str, float]:
        gpu_power = sum(
            gpu.power_usage_watts
            for node in self.nodes.values()
            if node.rack_id == rack_id
            for gpu in node.gpus
        )
        rack = self.racks[rack_id]
        non_gpu_it = 600.0 + gpu_power * 0.10
        it_total = gpu_power + non_gpu_it
        cooling = 350.0 + (
            it_total * 0.12 / max(rack.cooling_efficiency, 0.25)
        )
        facility_total = it_total + cooling
        return {
            "gpu": gpu_power,
            "non_gpu_it": non_gpu_it,
            "it_total": it_total,
            "cooling": cooling,
            "facility_total": facility_total,
        }

    def degrade_rack_cooling(
        self,
        rack_id: str,
        efficiency: float,
        *,
        actor: str = "system",
        request_id: str = "",
    ) -> None:
        with self._lock:
            rack = self.racks[rack_id]
            rack.cooling_efficiency = efficiency
            self._audit(
                "inject_scenario",
                {"scenario": "rack-a-cooling-degradation", "rack": rack_id, "efficiency": efficiency},
                actor,
                request_id,
            )
            self._persist_state()

    def restore_normal(self, *, actor: str = "system", request_id: str = "") -> None:
        with self._lock:
            for rack in self.racks.values():
                rack.cooling_efficiency = 0.85
                rack.health = "OK"
            for node in self.nodes.values():
                if node.unschedulable:
                    node.unschedulable = False
                    node.taints = []
            self._audit("restore_normal", {}, actor, request_id)
            self._persist_state()

    def submit_job(
        self,
        payload: dict,
        *,
        actor: str = "system",
        request_id: str = "",
    ) -> dict:
        with self._lock:
            job_id = max(self.jobs, default=100) + 1
            job = Job(
                job_id=job_id,
                job_name=payload.get("pod_name", payload.get("job_name", f"pod-{job_id}")),
                namespace=payload.get("namespace", "ml-workloads"),
                owner_kind=payload.get("owner_kind", "Job"),
                owner_name=payload.get("owner_name", payload.get("job_name", f"workload-{job_id}")),
                priority_class_name=payload.get("priority_class_name", "batch-normal"),
                state="Pending",
                reason="Resources",
                priority=int(payload.get("priority", 500)),
                requested_gpus=int(payload.get("requested_gpus", 1)),
                duration_seconds=int(payload.get("duration_seconds", 600)),
                container_name=payload.get("container_name", "main"),
                sla_class=payload.get("sla_class", "batch"),
                checkpointable=payload.get("checkpointable", True),
                checkpoint_interval_seconds=int(payload.get("checkpoint_interval_seconds", 300)),
                preemptible=payload.get("preemptible", True),
                flex_class=payload.get("flex_class", "flex_2"),
                max_throughput_reduction_pct=float(
                    payload.get("max_throughput_reduction_pct", 50.0)
                ),
                deadline_slack_seconds=int(
                    payload.get("deadline_slack_seconds", 3600)
                ),
                geo_shiftable=payload.get("geo_shiftable", False),
            )
            self.jobs[job_id] = job
            self._schedule_pending_jobs()
            self._audit("submit_workload", {"workload_id": job_id, "pod": job.job_name}, actor, request_id)
            self._persist_state()
            return job.to_dict()

    def flexibility_offer(self, rack_id: str) -> dict:
        with self._lock:
            return self.action_arbiter.flexibility_offer(
                self.snapshot(),
                rack_id,
            )

    def create_grid_event(
        self,
        payload: dict,
        *,
        actor: str = "system",
        request_id: str = "",
    ) -> dict:
        with self._lock:
            rack_id = payload["rack_id"]
            snapshot = self.snapshot()
            rack = snapshot["racks"].get(rack_id)
            if rack is None:
                raise KeyError(rack_id)
            if any(
                event.rack_id == rack_id
                and event.status in {"active", "under_delivering", "recovery"}
                for event in self.grid_events.values()
            ):
                raise ValueError(f"rack {rack_id} already has an active grid event")

            baseline = float(
                rack["environment_metrics"]["power_watts"]["reading"]
            )
            requested_watts = payload.get("requested_reduction_watts")
            if requested_watts is None:
                requested_watts = (
                    baseline * float(payload["requested_reduction_pct"]) / 100.0
                )
            requested_watts = float(requested_watts)
            event_id = payload.get("event_id") or f"grid-{uuid4()}"
            if event_id in self.grid_events:
                raise ValueError(f"grid event already exists: {event_id}")

            plan = self.action_arbiter.plan(
                snapshot,
                rack_id,
                requested_watts,
            )
            now = datetime.now(timezone.utc).isoformat()
            offer = plan["offer"]
            event = GridEvent(
                event_id=event_id,
                source=payload["source"],
                event_type=payload["event_type"],
                rack_id=rack_id,
                requested_reduction_watts=requested_watts,
                response_deadline_seconds=int(
                    payload["response_deadline_seconds"]
                ),
                duration_seconds=int(payload["duration_seconds"]),
                recovery_ramp_watts_per_second=float(
                    payload["recovery_ramp_watts_per_second"]
                ),
                status="rejected" if plan["status"] == "rejected" else "active",
                created_at=now,
                baseline_power_watts=baseline,
                target_power_watts=max(0.0, baseline - requested_watts),
                available_reduction_watts=float(
                    offer["available_reduction_watts"]
                ),
                activated_at=None if plan["status"] == "rejected" else now,
                activated_at_epoch=None
                if plan["status"] == "rejected"
                else time(),
                failure_reason=plan["reason"],
                actions=plan["actions"],
            )
            self.grid_events[event_id] = event
            if plan["status"] == "planned":
                self._apply_grid_actions(event, actor, request_id)
                self._update_racks(0.0)
                self._update_grid_event_tracking()
            self._audit(
                "grid_event_created",
                {
                    "event_id": event_id,
                    "rack": rack_id,
                    "status": event.status,
                    "requested_reduction_watts": requested_watts,
                    "available_reduction_watts": event.available_reduction_watts,
                    "failure_reason": event.failure_reason,
                },
                actor,
                request_id,
            )
            self._persist_state()
            return event.to_dict()

    def complete_grid_event(
        self,
        event_id: str,
        *,
        reason: str,
        actor: str = "system",
        request_id: str = "",
    ) -> dict:
        with self._lock:
            event = self.grid_events.get(event_id)
            if event is None:
                raise KeyError(event_id)
            if event.status not in {"active", "under_delivering"}:
                raise ValueError(
                    f"grid event {event_id} cannot complete from {event.status}"
                )
            self._start_grid_recovery(event)
            self._audit(
                "grid_event_recovery_started",
                {"event_id": event_id, "reason": reason},
                actor,
                request_id,
            )
            self._persist_state()
            return event.to_dict()

    def list_grid_events(self) -> list[dict]:
        with self._lock:
            return [
                event.to_dict()
                for event in sorted(
                    self.grid_events.values(),
                    key=lambda item: item.created_at,
                    reverse=True,
                )
            ]

    def _apply_grid_actions(
        self,
        event: GridEvent,
        actor: str,
        request_id: str,
    ) -> None:
        for action in event.actions:
            gpu = self._gpu_by_uuid(action["gpu_uuid"])
            if gpu is None:
                event.status = "failed"
                event.failure_reason = (
                    f"GPU disappeared before action: {action['gpu_uuid']}"
                )
                return
            gpu.power_limit_watts = float(action["new_power_limit_watts"])
            gpu.power_usage_watts = min(
                gpu.power_usage_watts,
                gpu.power_limit_watts,
            )
            self._audit(
                "set_gpu_power_limit",
                {
                    "event_id": event.event_id,
                    "workload_id": action["job_id"],
                    "gpu_uuid": gpu.uuid,
                    "power_limit_watts": gpu.power_limit_watts,
                },
                actor,
                request_id,
            )

    def _advance_grid_events(self, elapsed: float, now_epoch: float) -> None:
        for event in self.grid_events.values():
            if (
                event.status in {"active", "under_delivering"}
                and event.activated_at_epoch is not None
                and now_epoch - event.activated_at_epoch >= event.duration_seconds
            ):
                self._start_grid_recovery(event)
            if event.status != "recovery":
                continue
            per_gpu_ramp = (
                event.recovery_ramp_watts_per_second
                / max(len(event.actions), 1)
            )
            restored = True
            for action in event.actions:
                gpu = self._gpu_by_uuid(action["gpu_uuid"])
                if gpu is None:
                    continue
                original_limit = float(
                    action["original_power_limit_watts"]
                )
                gpu.power_limit_watts = min(
                    original_limit,
                    gpu.power_limit_watts + per_gpu_ramp * elapsed,
                )
                if gpu.power_limit_watts < original_limit - 0.01:
                    restored = False
            if restored:
                event.status = "closed"
                event.closed_at = datetime.now(timezone.utc).isoformat()

    def _start_grid_recovery(self, event: GridEvent) -> None:
        event.status = "recovery"
        event.recovery_started_at = datetime.now(timezone.utc).isoformat()

    def _update_grid_event_tracking(self) -> None:
        for event in self.grid_events.values():
            if event.status in {"rejected", "failed"}:
                continue
            current_power = self._rack_power(event.rack_id)
            event.achieved_reduction_watts = max(
                0.0,
                event.baseline_power_watts - current_power,
            )
            event.compliance_ratio = min(
                1.0,
                event.achieved_reduction_watts
                / max(event.requested_reduction_watts, 1.0),
            )
            if (
                event.compliance_ratio >= 0.95
                and event.response_time_seconds is None
                and event.activated_at_epoch is not None
            ):
                event.response_time_seconds = max(
                    0.0,
                    time() - event.activated_at_epoch,
                )
                event.response_deadline_met = (
                    event.response_time_seconds
                    <= event.response_deadline_seconds
                )
            elif (
                event.response_deadline_met is None
                and event.activated_at_epoch is not None
                and time() - event.activated_at_epoch
                > event.response_deadline_seconds
            ):
                event.response_deadline_met = False
            if event.status in {"active", "under_delivering"}:
                event.status = (
                    "active"
                    if event.compliance_ratio >= 0.95
                    else "under_delivering"
                )

    def _gpu_by_uuid(self, gpu_uuid: str) -> GPU | None:
        return next(
            (
                gpu
                for node in self.nodes.values()
                for gpu in node.gpus
                if gpu.uuid == gpu_uuid
            ),
            None,
        )

    def execute_recommendation(
        self,
        recommendation: dict,
        *,
        actor: str = "system",
        request_id: str = "",
    ) -> dict:
        with self._lock:
            job_id = recommendation["target"]["job_id"]
            node_name = recommendation["target"]["node"]
            job = self.jobs.get(job_id)
            node = self.nodes.get(node_name)
            invalid_reason = self._execution_precondition_failure(job, node, node_name)
            if invalid_reason:
                self._audit(
                    "action_blocked",
                    {"recommendation_id": recommendation["id"], "reason": invalid_reason},
                    actor,
                    request_id,
                )
                return {"status": "blocked", "reason": invalid_reason}

            assert job is not None
            assert node is not None
            self._audit("checkpoint_workload", {"workload_id": job_id, "pod": job.job_name}, actor, request_id)
            node.unschedulable = True
            node.taints = [
                {
                    "key": "policy.gpu-ops/thermal-protection",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ]
            self._audit("cordon_node", {"node": node_name}, actor, request_id)
            self._release_job(job)
            job.state = "Pending"
            job.reason = "Evicted"
            self._audit("evict_pod", {"workload_id": job_id, "pod": job.job_name}, actor, request_id)
            self._audit(
                "requeue_workload",
                {"workload_id": job_id, "owner_kind": job.owner_kind, "owner_name": job.owner_name},
                actor,
                request_id,
            )
            self._schedule_pending_jobs()
            self._persist_state()
            invariant_errors = self.validate_invariants()
            verification = {
                "node_cordoned": node.unschedulable,
                "workload_removed_from_target": job.node_name != node_name,
                "invariant_errors": invariant_errors,
            }
            verified = (
                verification["node_cordoned"]
                and verification["workload_removed_from_target"]
                and not invariant_errors
            )
            return {
                "status": "executed" if verified else "failed",
                "recommendation_id": recommendation["id"],
                "verification": verification,
            }

    def _execution_precondition_failure(
        self,
        job: Job | None,
        node: Node | None,
        expected_node_name: str,
    ) -> str | None:
        if job is None or node is None:
            return "target no longer exists"
        if job.state != "Running" or job.node_name != expected_node_name:
            return "workload is no longer running on the recommended node"
        if not job.checkpointable or not job.preemptible or job.sla_class == "production":
            return "guardrail prevented action"
        colocated = [
            candidate.job_id
            for candidate in self.jobs.values()
            if candidate.state == "Running"
            and candidate.node_name == expected_node_name
            and candidate.job_id != job.job_id
        ]
        if colocated:
            return f"full node evacuation blocked by colocated workloads: {colocated}"
        return None

    def snapshot(self) -> dict:
        with self._lock:
            rack_power = {
                rack_id: self._rack_power_components(rack_id)
                for rack_id in self.racks
            }
            return {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "gpu_profile": self.profile.__dict__,
                "racks": {
                    rack_id: rack.to_dict(
                        power_consumed_watts=rack_power[rack_id][
                            "facility_total"
                        ],
                        power_components=rack_power[rack_id],
                    )
                    for rack_id, rack in self.racks.items()
                },
                "nodes": {name: node.to_dict() for name, node in self.nodes.items()},
                "jobs": {job_id: job.to_dict() for job_id, job in self.jobs.items()},
                "grid_events": {
                    event_id: event.to_dict()
                    for event_id, event in self.grid_events.items()
                },
            }

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [job.to_dict() for job in self.jobs.values()]

    def export_state(self) -> dict:
        with self._lock:
            return {
                "state_version": self.STATE_VERSION,
                "seed": self.seed,
                "profile": asdict(self.profile),
                "racks": {rack_id: asdict(rack) for rack_id, rack in self.racks.items()},
                "nodes": {name: asdict(node) for name, node in self.nodes.items()},
                "jobs": {str(job_id): asdict(job) for job_id, job in self.jobs.items()},
                "grid_events": {
                    event_id: asdict(event)
                    for event_id, event in self.grid_events.items()
                },
            }

    def restore_state(self, state: dict) -> None:
        if state.get("state_version") != self.STATE_VERSION:
            raise ValueError("unsupported simulator state version")
        with self._lock:
            self.seed = int(state.get("seed", self.seed))
            self.random = random.Random(self.seed)
            self.profile = GPUProfile(**state["profile"])
            self.action_arbiter = ActionArbiter(
                minimum_gpu_power_limit_watts=self.profile.power_limit_min_watts,
                thermal_slowdown_temperature_c=self.profile.slowdown_temperature_c,
            )
            self.racks = {
                rack_id: Rack(**rack_data)
                for rack_id, rack_data in state["racks"].items()
            }
            self.nodes = {}
            for node_name, node_data in state["nodes"].items():
                gpus = [GPU(**gpu_data) for gpu_data in node_data.pop("gpus")]
                self.nodes[node_name] = Node(**node_data, gpus=gpus)
            self.jobs = {
                int(job_id): Job(**job_data)
                for job_id, job_data in state["jobs"].items()
            }
            self.grid_events = {
                event_id: GridEvent(**event_data)
                for event_id, event_data in state.get(
                    "grid_events",
                    {},
                ).items()
            }
            self._last_tick = time()

    def validate_invariants(self) -> list[str]:
        with self._lock:
            errors: list[str] = []
            running_ids = {job.job_id for job in self.jobs.values() if job.state == "Running"}
            for node in self.nodes.values():
                allocations = [
                    gpu.allocated_to_workload_id
                    for gpu in node.gpus
                    if gpu.allocated_to_workload_id is not None
                ]
                for workload_id in allocations:
                    if workload_id not in running_ids:
                        errors.append(f"{node.name} GPU allocated to non-running workload {workload_id}")
                expected_cpu = sum(
                    16 * job.requested_gpus
                    for job in self.jobs.values()
                    if job.state == "Running" and job.node_name == node.name
                )
                if node.cpus_allocated != expected_cpu:
                    errors.append(f"{node.name} CPU allocation is inconsistent")
            return errors

    def render_native_metrics(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# TYPE gpu_simulator_gpu_temperature_celsius gauge",
            "# TYPE gpu_simulator_gpu_power_usage_watts gauge",
            "# TYPE gpu_simulator_node_max_gpu_temperature_celsius gauge",
            "# TYPE gpu_simulator_rack_cooling_headroom_pct gauge",
            "# TYPE gpu_simulator_rack_facility_power_watts gauge",
            "# TYPE gpu_simulator_grid_event_target_reduction_watts gauge",
            "# TYPE gpu_simulator_grid_event_achieved_reduction_watts gauge",
            "# TYPE gpu_simulator_grid_event_compliance_ratio gauge",
        ]
        for node in snapshot["nodes"].values():
            node_name = node["metadata"]["name"]
            lines.append(f'gpu_simulator_node_max_gpu_temperature_celsius{{hostname="{node_name}"}} {node["derived"]["max_gpu_temperature_celsius"]:.2f}')
            for gpu in node["gpus"]:
                labels = f'hostname="{node_name}",gpu="{gpu["index"]}",uuid="{gpu["uuid"]}",model="{self.profile.model}"'
                lines.append(f"gpu_simulator_gpu_temperature_celsius{{{labels}}} {gpu['temperature_c']:.2f}")
                lines.append(f"gpu_simulator_gpu_power_usage_watts{{{labels}}} {gpu['power_usage_watts']:.2f}")
        for rack in snapshot["racks"].values():
            lines.append(f'gpu_simulator_rack_cooling_headroom_pct{{rack="{rack["id"]}"}} {rack["derived"]["cooling_headroom_pct"]:.2f}')
            lines.append(
                f'gpu_simulator_rack_facility_power_watts{{rack="{rack["id"]}"}} '
                f'{rack["environment_metrics"]["power_watts"]["reading"]:.2f}'
            )
        for event in snapshot["grid_events"].values():
            labels = (
                f'event_id="{event["event_id"]}",rack="{event["rack_id"]}",'
                f'status="{event["status"]}"'
            )
            lines.append(
                f"gpu_simulator_grid_event_target_reduction_watts{{{labels}}} "
                f'{event["requested_reduction_watts"]:.2f}'
            )
            lines.append(
                f"gpu_simulator_grid_event_achieved_reduction_watts{{{labels}}} "
                f'{event["achieved_reduction_watts"]:.2f}'
            )
            lines.append(
                f"gpu_simulator_grid_event_compliance_ratio{{{labels}}} "
                f'{event["compliance_ratio"]:.4f}'
            )
        return "\n".join(lines) + "\n"

    def render_dcgm_metrics(self) -> str:
        snapshot = self.snapshot()
        fields = [
            ("DCGM_FI_DEV_SM_CLOCK", "gauge", "SM clock frequency (in MHz).", lambda gpu: gpu["sm_clock_mhz"]),
            ("DCGM_FI_DEV_MEM_CLOCK", "gauge", "Memory clock frequency (in MHz).", lambda gpu: 1593 if gpu["utilization_pct"] > 5 else 405),
            ("DCGM_FI_DEV_MEMORY_TEMP", "gauge", "Memory temperature (in C).", lambda gpu: gpu["memory_temperature_c"]),
            ("DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (in C).", lambda gpu: gpu["temperature_c"]),
            ("DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (in W).", lambda gpu: gpu["power_usage_watts"]),
            ("DCGM_FI_DEV_POWER_MGMT_LIMIT", "gauge", "Current power limit (in W).", lambda gpu: gpu["power_limit_watts"]),
            ("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "counter", "Total energy consumption since boot (in mJ).", lambda gpu: gpu["total_energy_mj"]),
            ("DCGM_FI_DEV_GPU_UTIL", "gauge", "GPU utilization (in %).", lambda gpu: gpu["utilization_pct"]),
            ("DCGM_FI_DEV_MEM_COPY_UTIL", "gauge", "Memory utilization (in %).", lambda gpu: gpu["memory_copy_util_pct"]),
            ("DCGM_FI_DEV_FB_FREE", "gauge", "Framebuffer memory free (in MiB).", lambda gpu: max(0, self.profile.framebuffer_total_mib - gpu["memory_used_mib"])),
            ("DCGM_FI_DEV_FB_USED", "gauge", "Framebuffer memory used (in MiB).", lambda gpu: gpu["memory_used_mib"]),
            ("DCGM_FI_DEV_FB_RESERVED", "gauge", "Framebuffer memory reserved (in MiB).", lambda gpu: 0),
            ("DCGM_FI_DEV_XID_ERRORS", "gauge", "Value of the last XID error encountered.", lambda gpu: gpu["xid_errors"]),
            ("DCGM_FI_DEV_ECC_SBE_AGG_TOTAL", "counter", "Total single-bit volatile ECC errors.", lambda gpu: gpu["ecc_sbe_agg_total"]),
            ("DCGM_FI_DEV_ECC_DBE_AGG_TOTAL", "counter", "Total double-bit volatile ECC errors.", lambda gpu: gpu["ecc_dbe_agg_total"]),
        ]

        allocation_labels = {}
        for job in snapshot["jobs"].values():
            pod = job["kubernetes"]
            for gpu_uuid in job["allocated_gpu_uuids"]:
                allocation_labels[gpu_uuid] = {
                    "container": pod["spec"]["containers"][0]["name"],
                    "namespace": pod["metadata"]["namespace"],
                    "pod": pod["metadata"]["name"],
                }

        def escape_label(value: object) -> str:
            return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

        lines = []
        for metric_name, metric_type, help_text, value_getter in fields:
            lines.append(f"# HELP {metric_name} {help_text}")
            lines.append(f"# TYPE {metric_name} {metric_type}")
            for node in snapshot["nodes"].values():
                for gpu in node["gpus"]:
                    workload = allocation_labels.get(gpu["uuid"], {})
                    node_ordinal = int(node["metadata"]["name"].rsplit("-", 1)[-1])
                    bus = 0x20 + (node_ordinal - 1) * 8 + gpu["index"]
                    label_values = {
                        "gpu": gpu["index"],
                        "UUID": gpu["uuid"],
                        "pci_bus_id": f"00000000:{bus:02X}:00.0",
                        "device": f'nvidia{gpu["index"]}',
                        "modelName": self.profile.model,
                        "hostname": node["metadata"]["name"],
                    }
                    if workload:
                        label_values.update(workload)
                    if metric_name == "DCGM_FI_DEV_XID_ERRORS":
                        label_values.update(
                            {
                                "err_code": gpu["xid_errors"],
                                "err_msg": (
                                    "No XID error"
                                    if gpu["xid_errors"] == 0
                                    else "Simulated XID error"
                                ),
                            }
                        )
                    labels = ",".join(
                        f'{name}="{escape_label(value)}"'
                        for name, value in label_values.items()
                    )
                    lines.append(f"{metric_name}{{{labels}}} {value_getter(gpu)}")
        return "\n".join(lines) + "\n"

    def _audit(
        self,
        action: str,
        detail: dict,
        actor: str = "system",
        request_id: str = "",
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "request_id": request_id,
            "action": action,
            "detail": detail,
        }
        self._recent_audit.append(event)
        if self._audit_sink:
            self._audit_sink(action, detail, actor, request_id)

    def _persist_state(self) -> None:
        if self._state_sink:
            self._state_sink(self.export_state())
