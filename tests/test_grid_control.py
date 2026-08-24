from app.simulator.cluster import ClusterSimulator


def grid_event_payload(**overrides):
    payload = {
        "event_id": "grid-test-1",
        "source": "test-grid",
        "event_type": "emergency_curtailment",
        "rack_id": "rack-a",
        "requested_reduction_pct": 30.0,
        "response_deadline_seconds": 40,
        "duration_seconds": 7200,
        "recovery_ramp_watts_per_second": 100.0,
    }
    payload.update(overrides)
    return payload


def test_grid_event_meets_target_without_touching_production_workload():
    simulator = ClusterSimulator(seed=42)
    production = next(
        job for job in simulator.jobs.values() if job.sla_class == "production"
    )
    production_gpu = simulator._gpu_by_uuid(production.allocated_gpu_uuids[0])

    event = simulator.create_grid_event(grid_event_payload())

    assert event["status"] == "active"
    assert event["compliance_ratio"] >= 0.95
    assert event["achieved_reduction_watts"] >= (
        event["requested_reduction_watts"] * 0.95
    )
    assert event["response_deadline_met"] is True
    assert event["response_time_seconds"] <= 40
    assert production_gpu is not None
    assert production_gpu.power_limit_watts == simulator.profile.power_limit_default_watts
    assert all(
        action["workload"] != production.job_name
        for action in event["actions"]
    )


def test_grid_event_recovery_restores_original_power_limits():
    simulator = ClusterSimulator(seed=42)
    created = simulator.create_grid_event(grid_event_payload())

    recovering = simulator.complete_grid_event(
        created["event_id"],
        reason="grid released the event",
    )
    simulator._last_tick -= 120
    simulator.tick()
    closed = simulator.grid_events[created["event_id"]]

    assert recovering["status"] == "recovery"
    assert closed.status == "closed"
    for action in closed.actions:
        gpu = simulator._gpu_by_uuid(action["gpu_uuid"])
        assert gpu is not None
        assert gpu.power_limit_watts == action["original_power_limit_watts"]


def test_operational_safety_rejects_grid_control():
    simulator = ClusterSimulator(seed=42)
    simulator.degrade_rack_cooling("rack-a", 0.02)

    event = simulator.create_grid_event(grid_event_payload())

    assert event["status"] == "rejected"
    assert "operational safety owns control" in event["failure_reason"]
    assert event["actions"] == []


def test_grid_event_survives_state_round_trip():
    simulator = ClusterSimulator(seed=42)
    created = simulator.create_grid_event(grid_event_payload())
    restored = ClusterSimulator(seed=7)

    restored.restore_state(simulator.export_state())

    assert restored.grid_events[created["event_id"]].status == "active"
    assert restored.snapshot()["grid_events"][created["event_id"]] == created
