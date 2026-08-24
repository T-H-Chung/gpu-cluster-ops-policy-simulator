import pytest
from pydantic import ValidationError

from app.schemas import GridEventCreateRequest, WorkloadCreateRequest


@pytest.mark.parametrize(
    "payload",
    [
        {"requested_gpus": 0},
        {"duration_seconds": 0},
        {"checkpointable": "false"},
        {"preemptible": "true"},
        {"unexpected": "field"},
        {"job_name": "Uppercase-Is-Invalid"},
    ],
)
def test_invalid_workload_payloads_are_rejected(payload):
    with pytest.raises(ValidationError):
        WorkloadCreateRequest.model_validate(payload)


def test_boolean_payloads_remain_boolean():
    workload = WorkloadCreateRequest(
        checkpointable=False,
        preemptible=False,
    )

    assert workload.checkpointable is False
    assert workload.preemptible is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "requested_reduction_watts": 1000,
            "requested_reduction_pct": 20,
        },
        {"requested_reduction_pct": 0},
        {"requested_reduction_pct": 101},
        {"requested_reduction_watts": "1000"},
        {
            "event_id": "bad\"metric",
            "requested_reduction_pct": 30,
        },
    ],
)
def test_invalid_grid_event_targets_are_rejected(payload):
    with pytest.raises(ValidationError):
        GridEventCreateRequest.model_validate(payload)


def test_grid_event_accepts_one_reduction_target():
    event = GridEventCreateRequest(requested_reduction_pct=30)

    assert event.requested_reduction_pct == 30
    assert event.requested_reduction_watts is None
