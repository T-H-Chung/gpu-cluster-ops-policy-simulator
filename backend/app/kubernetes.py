from __future__ import annotations

from copy import deepcopy
from typing import Any


API_VERSION = "v1"


def pod_resource(workload: dict[str, Any]) -> dict[str, Any]:
    """Return a Pod-shaped simulator workload as a Kubernetes core/v1 Pod."""
    source = workload["kubernetes"]
    pod = deepcopy(source)
    pod["apiVersion"] = API_VERSION
    pod["kind"] = "Pod"
    metadata = pod["metadata"]
    metadata.setdefault("resourceVersion", "1")

    for owner in metadata.get("ownerReferences", []):
        owner.setdefault(
            "apiVersion",
            "batch/v1" if owner.get("kind") == "Job" else "apps/v1",
        )
        owner.setdefault("uid", f'{owner.get("kind", "owner").lower()}-{owner["name"]}')
        owner.setdefault("controller", True)
        owner.setdefault("blockOwnerDeletion", True)

    for container in pod["spec"].get("containers", []):
        resources = container.get("resources", {})
        for resource_set in ("requests", "limits"):
            quantities = resources.get(resource_set, {})
            if "nvidia.com/gpu" in quantities:
                quantities["nvidia.com/gpu"] = str(quantities["nvidia.com/gpu"])

    status = pod.get("status", {})
    status.pop("elapsedSeconds", None)
    if status.get("reason") is None:
        status.pop("reason", None)
    return pod


def node_resource(node: dict[str, Any]) -> dict[str, Any]:
    """Return a simulator node using Kubernetes core/v1 Node semantics."""
    capacity = {
        name: str(value) for name, value in node["status"]["capacity"].items()
    }
    # Kubernetes allocatable is node capacity available to the scheduler. It is
    # not decremented for resources already requested by bound Pods.
    allocatable = dict(capacity)
    return {
        "apiVersion": API_VERSION,
        "kind": "Node",
        "metadata": {
            **deepcopy(node["metadata"]),
            "uid": f'node-{node["metadata"]["name"]}',
            "resourceVersion": "1",
        },
        "spec": deepcopy(node["spec"]),
        "status": {
            "conditions": deepcopy(node["status"]["conditions"]),
            "capacity": capacity,
            "allocatable": allocatable,
        },
    }


def resource_list(kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": f"{kind}List",
        "metadata": {"resourceVersion": "1"},
        "items": items,
    }


def api_resource_list() -> dict[str, Any]:
    return {
        "kind": "APIResourceList",
        "apiVersion": "v1",
        "groupVersion": "v1",
        "resources": [
            {
                "name": "pods",
                "singularName": "pod",
                "namespaced": True,
                "kind": "Pod",
                "verbs": ["create", "get", "list"],
                "shortNames": ["po"],
            },
            {
                "name": "nodes",
                "singularName": "node",
                "namespaced": False,
                "kind": "Node",
                "verbs": ["get", "list"],
                "shortNames": ["no"],
            },
        ],
    }


def pod_create_to_workload(payload: dict[str, Any], namespace: str) -> dict[str, Any]:
    """Translate a core/v1 Pod create request into simulator workload input."""
    if payload.get("apiVersion") != "v1" or payload.get("kind") != "Pod":
        raise ValueError("apiVersion must be v1 and kind must be Pod")
    metadata = payload.get("metadata")
    spec = payload.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ValueError("metadata and spec are required objects")
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("metadata.name is required")
    supplied_namespace = metadata.get("namespace", namespace)
    if supplied_namespace != namespace:
        raise ValueError("metadata.namespace must match the request namespace")

    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise ValueError("the supported Pod subset requires exactly one container")
    requested_gpus = 0
    for container in containers:
        if not isinstance(container, dict):
            raise ValueError("each container must be an object")
        if not isinstance(container.get("name"), str) or not container["name"]:
            raise ValueError("each container requires a name")
        resources = container.get("resources", {})
        if not isinstance(resources, dict):
            raise ValueError("container resources must be an object")
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        if not isinstance(requests, dict) or not isinstance(limits, dict):
            raise ValueError("container resource requests and limits must be objects")
        requested = _quantity(requests.get("nvidia.com/gpu", 0), "GPU request")
        limited = _quantity(limits.get("nvidia.com/gpu", requested), "GPU limit")
        if requested and limited != requested:
            raise ValueError("nvidia.com/gpu request and limit must be equal")
        requested_gpus += limited
    if requested_gpus < 1:
        raise ValueError("the Pod must request nvidia.com/gpu")

    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict):
        raise ValueError("metadata.annotations must be an object")
    owners = metadata.get("ownerReferences", [])
    owner = owners[0] if isinstance(owners, list) and owners else {}
    if not isinstance(owner, dict):
        raise ValueError("metadata.ownerReferences entries must be objects")
    owner_kind = owner.get("kind", "Job")
    if owner_kind not in {"Job", "Deployment", "StatefulSet"}:
        raise ValueError("owner kind must be Job, Deployment, or StatefulSet")

    return {
        "pod_name": name,
        "job_name": owner.get("name", name),
        "namespace": namespace,
        "owner_kind": owner_kind,
        "owner_name": owner.get("name", name),
        "priority_class_name": spec.get("priorityClassName", "batch-normal"),
        "priority": int(spec.get("priority", 500)),
        "requested_gpus": requested_gpus,
        "container_name": containers[0]["name"],
        "duration_seconds": _annotation_int(
            annotations, "simulator.gpu-ops/duration-seconds", 600
        ),
        "sla_class": annotations.get("policy.gpu-ops/sla-class", "batch"),
        "checkpointable": _annotation_bool(
            annotations, "policy.gpu-ops/checkpointable", True
        ),
        "checkpoint_interval_seconds": _annotation_int(
            annotations, "policy.gpu-ops/checkpoint-interval-seconds", 300
        ),
        "preemptible": _annotation_bool(
            annotations, "policy.gpu-ops/preemptible", True
        ),
        "flex_class": annotations.get("policy.gpu-ops/flex-class", "flex_2"),
        "max_throughput_reduction_pct": _annotation_float(
            annotations, "policy.gpu-ops/max-throughput-reduction-pct", 50.0
        ),
        "deadline_slack_seconds": _annotation_int(
            annotations, "policy.gpu-ops/deadline-slack-seconds", 3600
        ),
        "geo_shiftable": _annotation_bool(
            annotations, "policy.gpu-ops/geo-shiftable", False
        ),
    }


def status_failure(message: str, *, reason: str, code: int) -> dict[str, Any]:
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "message": message,
        "reason": reason,
        "code": code,
    }


def _quantity(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an integer quantity") from error
    if parsed < 0 or str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise ValueError(f"{label} must be a non-negative integer quantity")
    return parsed


def _annotation_bool(annotations: dict[str, Any], key: str, default: bool) -> bool:
    value = annotations.get(key)
    if value is None:
        return default
    if value not in {"true", "false"}:
        raise ValueError(f"annotation {key} must be 'true' or 'false'")
    return value == "true"


def _annotation_int(annotations: dict[str, Any], key: str, default: int) -> int:
    value = annotations.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"annotation {key} must be an integer") from error


def _annotation_float(annotations: dict[str, Any], key: str, default: float) -> float:
    value = annotations.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"annotation {key} must be a number") from error
