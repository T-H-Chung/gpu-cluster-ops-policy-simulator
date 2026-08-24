from app.simulator.cluster import ClusterSimulator


def test_releasing_one_workload_preserves_other_allocations():
    simulator = ClusterSimulator(seed=42)
    node = next(
        node
        for node in simulator.nodes.values()
        if len([job for job in simulator.jobs.values() if job.node_name == node.name]) >= 2
    )
    workloads = [
        job for job in simulator.jobs.values() if job.node_name == node.name
    ]
    victim, survivor = workloads[:2]

    simulator._release_job(victim)

    assert survivor.node_name == node.name
    assert node.allocated_gpu_count() == survivor.requested_gpus
    assert all(
        gpu.allocated_to_workload_id == survivor.job_id
        for gpu in node.gpus
        if gpu.allocated_to_workload_id is not None
    )
    assert simulator.validate_invariants() == []


def test_executor_revalidates_workload_location():
    simulator = ClusterSimulator(seed=42)
    job = next(job for job in simulator.jobs.values() if job.checkpointable)
    recommendation = {
        "id": "rec-test",
        "target": {"job_id": job.job_id, "node": "gpu-node-99"},
    }

    result = simulator.execute_recommendation(recommendation)

    assert result["status"] == "blocked"


def test_executor_blocks_partial_node_evacuation():
    simulator = ClusterSimulator(seed=42)
    node = next(
        node
        for node in simulator.nodes.values()
        if len([job for job in simulator.jobs.values() if job.node_name == node.name]) >= 2
    )
    job = next(job for job in simulator.jobs.values() if job.node_name == node.name)
    recommendation = {
        "id": "rec-test",
        "target": {"job_id": job.job_id, "node": node.name},
    }

    result = simulator.execute_recommendation(recommendation)

    assert result["status"] == "blocked"
    assert "colocated workloads" in result["reason"]


def test_state_round_trip():
    simulator = ClusterSimulator(seed=42)
    simulator.degrade_rack_cooling("rack-a", 0.25)
    state = simulator.export_state()
    restored = ClusterSimulator(seed=7)

    restored.restore_state(state)

    assert restored.racks["rack-a"].cooling_efficiency == 0.25
    assert restored.snapshot()["jobs"] == simulator.snapshot()["jobs"]
    assert restored.validate_invariants() == []
