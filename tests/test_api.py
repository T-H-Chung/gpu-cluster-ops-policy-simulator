from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def settings(database_path):
    return Settings(
        database_path=database_path,
        auth_enabled=True,
        api_keys={
            "viewer": "viewer-secret",
            "operator": "operator-secret",
            "admin": "admin-secret",
        },
        simulator_seed=42,
        audit_retention_days=90,
        audit_max_rows=100_000,
    )


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_rbac_validation_health_and_request_id(tmp_path):
    application = create_app(settings(tmp_path / "api.db"))
    with TestClient(application) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/v1/cluster").status_code == 401
        assert (
            client.get(
                "/api/v1/cluster",
                headers=auth("viewer-secret"),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/workloads",
                headers=auth("viewer-secret"),
                json={"job_name": "denied"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/recommendations/rec-test/approve",
                headers=auth("operator-secret"),
                json={"reason": "operator must not approve"},
            ).status_code
            == 403
        )
        invalid = client.post(
            "/api/v1/workloads",
            headers=auth("operator-secret"),
            json={"job_name": "bad-input", "checkpointable": "false"},
        )
        assert invalid.status_code == 422
        created = client.post(
            "/api/v1/workloads",
            headers={
                **auth("operator-secret"),
                "X-Request-ID": "request-test-1",
            },
            json={"job_name": "accepted-workload", "requested_gpus": 1},
        )
        assert created.status_code == 201
        assert created.headers["X-Request-ID"] == "request-test-1"

        audit = client.get(
            "/api/v1/audit",
            headers=auth("viewer-secret"),
        ).json()
        submitted = next(item for item in audit if item["action"] == "submit_workload")
        assert submitted["actor"] == "api-key:operator"
        assert submitted["request_id"] == "request-test-1"


def test_simulator_state_survives_app_restart(tmp_path):
    configured = settings(tmp_path / "persistent.db")
    first_app = create_app(configured)
    with TestClient(first_app) as client:
        response = client.post(
            "/api/v1/workloads",
            headers=auth("operator-secret"),
            json={"job_name": "persistent-workload", "duration_seconds": 3600},
        )
        assert response.status_code == 201

    second_app = create_app(configured)
    with TestClient(second_app) as client:
        workloads = client.get(
            "/api/v1/workloads",
            headers=auth("viewer-secret"),
        ).json()

    assert any(
        item["kubernetes"]["metadata"]["name"] == "persistent-workload"
        for item in workloads
    )


def test_grid_event_api_is_guarded_and_audited(tmp_path):
    application = create_app(settings(tmp_path / "grid-api.db"))
    payload = {
        "event_id": "grid-api-1",
        "source": "test-grid",
        "event_type": "emergency_curtailment",
        "rack_id": "rack-a",
        "requested_reduction_pct": 30,
        "response_deadline_seconds": 40,
        "duration_seconds": 7200,
        "recovery_ramp_watts_per_second": 100,
    }
    with TestClient(application) as client:
        offer = client.get(
            "/api/v1/flexibility",
            headers=auth("viewer-secret"),
        )
        denied = client.post(
            "/api/v1/grid-events",
            headers=auth("operator-secret"),
            json=payload,
        )
        created = client.post(
            "/api/v1/grid-events",
            headers={
                **auth("admin-secret"),
                "X-Request-ID": "grid-request-1",
            },
            json=payload,
        )
        listed = client.get(
            "/api/v1/grid-events",
            headers=auth("viewer-secret"),
        )
        completed = client.post(
            "/api/v1/grid-events/grid-api-1/complete",
            headers=auth("admin-secret"),
            json={"reason": "grid event released"},
        )
        audit = client.get(
            "/api/v1/audit",
            headers=auth("viewer-secret"),
        ).json()

    assert offer.status_code == 200
    assert offer.json()["eligible_gpu_count"] > 0
    assert denied.status_code == 403
    assert created.status_code == 201
    assert created.json()["status"] == "active"
    assert listed.json()[0]["event_id"] == "grid-api-1"
    assert completed.json()["status"] == "recovery"
    event_audit = next(
        item for item in audit if item["action"] == "grid_event_created"
    )
    assert event_audit["actor"] == "api-key:admin"
    assert event_audit["request_id"] == "grid-request-1"
