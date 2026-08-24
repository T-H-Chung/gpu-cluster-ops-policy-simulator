from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    field_validator,
    model_validator,
)


class WorkloadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pod_name: str | None = Field(default=None, min_length=1, max_length=253)
    job_name: str | None = Field(default=None, min_length=1, max_length=253)
    namespace: str = Field(default="ml-workloads", min_length=1, max_length=63)
    owner_kind: Literal["Job", "Deployment", "StatefulSet"] = "Job"
    owner_name: str | None = Field(default=None, min_length=1, max_length=253)
    priority_class_name: str = Field(default="batch-normal", min_length=1, max_length=253)
    priority: int = Field(default=500, ge=0, le=1_000_000)
    requested_gpus: int = Field(default=1, ge=1, le=64)
    container_name: str = Field(default="main", min_length=1, max_length=63)
    duration_seconds: int = Field(default=600, ge=1, le=31_536_000)
    sla_class: Literal["batch", "production"] = "batch"
    checkpointable: StrictBool = True
    checkpoint_interval_seconds: int = Field(default=300, ge=1, le=86_400)
    preemptible: StrictBool = True
    flex_class: Literal["flex_0", "flex_1", "flex_2", "flex_3"] = "flex_2"
    max_throughput_reduction_pct: float = Field(default=50.0, ge=0, le=100)
    deadline_slack_seconds: int = Field(default=3600, ge=0, le=31_536_000)
    geo_shiftable: StrictBool = False

    @field_validator(
        "pod_name", "job_name", "namespace", "owner_name", "container_name"
    )
    @classmethod
    def validate_kubernetes_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.lower() or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in value
        ):
            raise ValueError("must contain only letters, numbers, '-' or '.'")
        return value


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=1000)


class GridEventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    source: str = Field(default="manual", min_length=1, max_length=128)
    event_type: Literal[
        "emergency_curtailment",
        "peak_demand",
        "price_spike",
        "carbon_aware",
    ] = "emergency_curtailment"
    rack_id: str = Field(default="rack-a", min_length=1, max_length=128)
    requested_reduction_watts: StrictFloat | None = Field(default=None, gt=0)
    requested_reduction_pct: StrictFloat | None = Field(default=None, gt=0, le=100)
    response_deadline_seconds: int = Field(default=40, ge=1, le=3600)
    duration_seconds: int = Field(default=7200, ge=1, le=86_400)
    recovery_ramp_watts_per_second: float = Field(default=100.0, gt=0)

    @field_validator("event_id", "rack_id")
    @classmethod
    def validate_grid_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789-."
        if (
            value != value.lower()
            or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
            or any(character not in allowed for character in value)
        ):
            raise ValueError(
                "must start with a letter or number and contain only "
                "lowercase letters, numbers, '-' or '.'"
            )
        return value

    @model_validator(mode="after")
    def validate_reduction_target(self) -> "GridEventCreateRequest":
        supplied = (
            self.requested_reduction_watts is not None,
            self.requested_reduction_pct is not None,
        )
        if sum(supplied) != 1:
            raise ValueError(
                "provide exactly one of requested_reduction_watts or "
                "requested_reduction_pct"
            )
        return self


class GridEventCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=3, max_length=1000)
