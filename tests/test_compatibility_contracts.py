import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.simulator.cluster import ClusterSimulator


def settings(database_path):
    return Settings(
        database_path=database_path,
        auth_enabled=False,
        api_keys={},
        simulator_seed=42,
        audit_retention_days=90,
        audit_max_rows=100_000,
    )


def test_dcgm_endpoint_has_exporter_metadata_and_kubernetes_labels(tmp_path):
    application = create_app(settings(tmp_path / "contract.db"))
    with TestClient(application) as client:
        response = client.get("/metrics")
        legacy_response = client.get("/metrics/dcgm")
        native_response = client.get("/metrics/simulator")

    assert response.status_code == 200
    assert response.text == legacy_response.text
    assert "DCGM_FI_DEV_GPU_TEMP" in response.text
    assert "gpu_simulator_gpu_temperature_celsius" not in response.text
    assert "gpu_simulator_gpu_temperature_celsius" in native_response.text

    expected_types = {
        "DCGM_FI_DEV_SM_CLOCK": "gauge",
        "DCGM_FI_DEV_MEM_CLOCK": "gauge",
        "DCGM_FI_DEV_GPU_TEMP": "gauge",
        "DCGM_FI_DEV_POWER_USAGE": "gauge",
        "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION": "counter",
        "DCGM_FI_DEV_FB_FREE": "gauge",
        "DCGM_FI_DEV_FB_USED": "gauge",
    }
    for name, metric_type in expected_types.items():
        assert f"# HELP {name} " in response.text
        assert f"# TYPE {name} {metric_type}" in response.text

    gpu_temp_series = [
        line
        for line in response.text.splitlines()
        if line.startswith("DCGM_FI_DEV_GPU_TEMP{")
    ]
    assert len(gpu_temp_series) == 8
    for line in gpu_temp_series:
        for label in (
            "gpu",
            "UUID",
            "pci_bus_id",
            "device",
            "modelName",
            "hostname",
        ):
            assert re.search(rf'[,{{]{label}="[^"]*"', line)
    assert any(
        all(f'{label}="' in line for label in ("pod", "namespace", "container"))
        for line in gpu_temp_series
    )


def test_kubernetes_discovery_and_read_contract(tmp_path):
    application = create_app(settings(tmp_path / "kubernetes.db"))
    with TestClient(application) as client:
        versions = client.get("/api").json()
        resources = client.get("/api/v1").json()
        pods = client.get("/api/v1/pods").json()
        nodes = client.get("/api/v1/nodes").json()
        one_pod = client.get(
            "/api/v1/namespaces/ml-workloads/pods/llama-training-pod"
        ).json()

    assert versions["kind"] == "APIVersions"
    assert versions["versions"] == ["v1"]
    assert resources["kind"] == "APIResourceList"
    assert {item["name"] for item in resources["resources"]} == {"pods", "nodes"}

    assert pods["apiVersion"] == "v1"
    assert pods["kind"] == "PodList"
    assert pods["metadata"]["resourceVersion"]
    assert len(pods["items"]) == 6
    assert one_pod["kind"] == "Pod"
    assert "kubernetes" not in one_pod
    assert "policy_metadata" not in one_pod
    assert "elapsedSeconds" not in one_pod["status"]
    assert isinstance(
        one_pod["spec"]["containers"][0]["resources"]["limits"][
            "nvidia.com/gpu"
        ],
        str,
    )

    assert nodes["apiVersion"] == "v1"
    assert nodes["kind"] == "NodeList"
    assert len(nodes["items"]) == 4
    for node in nodes["items"]:
        assert node["kind"] == "Node"
        assert node["status"]["allocatable"] == node["status"]["capacity"]
        assert "allocated" not in node["status"]
        assert isinstance(node["status"]["capacity"]["nvidia.com/gpu"], str)


def test_kubernetes_pod_create_and_status_errors(tmp_path):
    application = create_app(settings(tmp_path / "pod-create.db"))
    payload = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "contract-gpu-pod",
            "namespace": "ml-workloads",
            "annotations": {
                "policy.gpu-ops/sla-class": "batch",
                "policy.gpu-ops/checkpointable": "true",
                "policy.gpu-ops/preemptible": "true",
                "policy.gpu-ops/flex-class": "flex_2",
            },
        },
        "spec": {
            "priority": 100,
            "priorityClassName": "batch-low",
            "containers": [
                {
                    "name": "trainer",
                    "resources": {
                        "requests": {"nvidia.com/gpu": "1"},
                        "limits": {"nvidia.com/gpu": "1"},
                    },
                }
            ],
        },
    }
    with TestClient(application) as client:
        created = client.post(
            "/api/v1/namespaces/ml-workloads/pods", json=payload
        )
        duplicate = client.post(
            "/api/v1/namespaces/ml-workloads/pods", json=payload
        )
        missing = client.get(
            "/api/v1/namespaces/ml-workloads/pods/does-not-exist"
        )

    assert created.status_code == 201
    assert created.json()["apiVersion"] == "v1"
    assert created.json()["kind"] == "Pod"
    assert created.json()["metadata"]["name"] == "contract-gpu-pod"
    assert created.json()["spec"]["containers"][0]["name"] == "trainer"
    assert duplicate.status_code == 409
    assert duplicate.json()["kind"] == "Status"
    assert duplicate.json()["reason"] == "AlreadyExists"
    assert missing.status_code == 404
    assert missing.json()["kind"] == "Status"
    assert missing.json()["reason"] == "NotFound"


def test_internal_allocatable_semantics_remain_separate_from_kubernetes_contract():
    simulator = ClusterSimulator(seed=42)
    internal_nodes = simulator.snapshot()["nodes"].values()

    assert any(
        node["status"]["allocatable"]["nvidia.com/gpu"]
        < node["status"]["capacity"]["nvidia.com/gpu"]
        for node in internal_nodes
    )


def test_provisioned_grafana_dashboard_queries_emitted_metrics():
    root = Path(__file__).resolve().parents[1]
    dashboard = json.loads(
        (root / "grafana/dashboards/gpu-cluster-overview.json").read_text()
    )
    metric_text = ClusterSimulator(seed=42).render_dcgm_metrics()
    native_text = ClusterSimulator(seed=42).render_native_metrics()
    emitted_names = {
        line.split("{", 1)[0]
        for line in (metric_text + native_text).splitlines()
        if line and not line.startswith("#")
    }
    expressions = [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]

    assert dashboard["uid"] == "gpu-cluster-ops-overview"
    assert dashboard["templating"]["list"][0]["name"] == "hostname"
    for expression in expressions:
        metric_name = expression.split("{", 1)[0]
        assert metric_name in emitted_names
