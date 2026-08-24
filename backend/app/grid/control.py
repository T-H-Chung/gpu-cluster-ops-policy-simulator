from __future__ import annotations

from typing import Any


class ActionArbiter:
    """Build grid-response plans without overriding operational safety."""

    def __init__(
        self,
        *,
        minimum_gpu_power_limit_watts: float,
        thermal_slowdown_temperature_c: float,
    ) -> None:
        self.minimum_gpu_power_limit_watts = minimum_gpu_power_limit_watts
        self.thermal_slowdown_temperature_c = thermal_slowdown_temperature_c

    def flexibility_offer(self, snapshot: dict, rack_id: str) -> dict[str, Any]:
        rack = snapshot["racks"].get(rack_id)
        if rack is None:
            raise KeyError(rack_id)

        safety_blocks = self._safety_blocks(snapshot, rack_id)
        resources: list[dict[str, Any]] = []
        protected_workloads: list[str] = []
        for job in snapshot["jobs"].values():
            pod = job["kubernetes"]
            metadata = job["policy_metadata"]
            node_name = pod["spec"]["nodeName"]
            if (
                pod["status"]["phase"] != "Running"
                or node_name is None
                or snapshot["nodes"][node_name]["rack_id"] != rack_id
            ):
                continue
            if (
                metadata["flex_class"] == "flex_0"
                or metadata["sla_class"] == "production"
                or not metadata["preemptible"]
            ):
                protected_workloads.append(pod["metadata"]["name"])
                continue
            node = snapshot["nodes"][node_name]
            for gpu in node["gpus"]:
                if gpu["uuid"] not in job["allocated_gpu_uuids"]:
                    continue
                minimum_limit = self._minimum_limit_for_class(
                    metadata["flex_class"],
                    float(gpu["power_usage_watts"]),
                )
                available = max(
                    0.0,
                    float(gpu["power_usage_watts"]) - minimum_limit,
                )
                if available <= 0:
                    continue
                resources.append(
                    {
                        "job_id": int(
                            pod["metadata"]["labels"][
                                "simulator.gpu-ops/workload-id"
                            ]
                        ),
                        "workload": pod["metadata"]["name"],
                        "priority": int(pod["spec"]["priority"]),
                        "node": node_name,
                        "gpu_uuid": gpu["uuid"],
                        "current_power_usage_watts": float(
                            gpu["power_usage_watts"]
                        ),
                        "original_power_limit_watts": float(
                            gpu["power_limit_watts"]
                        ),
                        "minimum_power_limit_watts": minimum_limit,
                        "available_reduction_watts": available,
                        "flex_class": metadata["flex_class"],
                    }
                )

        resources.sort(key=lambda item: (item["priority"], item["workload"], item["gpu_uuid"]))
        available_reduction = sum(
            item["available_reduction_watts"] for item in resources
        )
        return {
            "rack_id": rack_id,
            "facility_power_watts": rack["environment_metrics"]["power_watts"][
                "reading"
            ],
            "available_reduction_watts": available_reduction,
            "available_reduction_pct": (
                available_reduction
                / max(
                    rack["environment_metrics"]["power_watts"]["reading"],
                    1.0,
                )
                * 100.0
            ),
            "eligible_gpu_count": len(resources),
            "protected_workloads": sorted(protected_workloads),
            "safety_blocks": safety_blocks,
            "resources": resources,
        }

    def plan(
        self,
        snapshot: dict,
        rack_id: str,
        requested_reduction_watts: float,
    ) -> dict[str, Any]:
        offer = self.flexibility_offer(snapshot, rack_id)
        if offer["safety_blocks"]:
            return {
                "status": "rejected",
                "reason": "; ".join(offer["safety_blocks"]),
                "offer": offer,
                "actions": [],
            }
        if requested_reduction_watts > offer["available_reduction_watts"]:
            return {
                "status": "rejected",
                "reason": "insufficient flexible GPU power",
                "offer": offer,
                "actions": [],
            }

        remaining = requested_reduction_watts
        actions: list[dict[str, Any]] = []
        for resource in offer["resources"]:
            if remaining <= 0.01:
                break
            reduction = min(resource["available_reduction_watts"], remaining)
            actions.append(
                {
                    "action": "set_gpu_power_limit",
                    "job_id": resource["job_id"],
                    "workload": resource["workload"],
                    "node": resource["node"],
                    "gpu_uuid": resource["gpu_uuid"],
                    "original_power_limit_watts": resource[
                        "original_power_limit_watts"
                    ],
                    "new_power_limit_watts": max(
                        resource["minimum_power_limit_watts"],
                        resource["current_power_usage_watts"] - reduction,
                    ),
                    "planned_reduction_watts": reduction,
                }
            )
            remaining -= reduction
        return {
            "status": "planned",
            "reason": None,
            "offer": offer,
            "actions": actions,
        }

    def _minimum_limit_for_class(
        self,
        flex_class: str,
        current_power_usage_watts: float,
    ) -> float:
        if flex_class == "flex_1":
            return max(
                self.minimum_gpu_power_limit_watts,
                current_power_usage_watts * 0.75,
            )
        return self.minimum_gpu_power_limit_watts

    def _safety_blocks(self, snapshot: dict, rack_id: str) -> list[str]:
        rack = snapshot["racks"][rack_id]
        blocks = []
        if rack["derived"]["cooling_headroom_pct"] < 5.0:
            blocks.append("operational safety owns control: cooling headroom is below 5%")
        hottest = max(
            (
                node["derived"]["max_gpu_temperature_celsius"]
                for node in snapshot["nodes"].values()
                if node["rack_id"] == rack_id
            ),
            default=0.0,
        )
        if hottest >= self.thermal_slowdown_temperature_c:
            blocks.append(
                "operational safety owns control: a GPU is at the thermal slowdown threshold"
            )
        return blocks
