# Architecture

## Goal

Simulate GPU cluster operations on a laptop while preserving realistic operational contracts:

- telemetry source semantics
- time-series behavior
- cross-layer correlations
- policy-based recommendations
- guarded action execution
- auditability

## Data Layers

```text
Raw compatible layer
DCGM-style metrics / Kubernetes-style Pod and Node records / Redfish-style resources
  -> Normalized state layer
GPU / Node / Rack / Workload / Incident
  -> Decision-derived layer
thermal_state / power_headroom / migration_feasibility / telemetry_age
```

The layers are intentionally separate. For example:

```yaml
raw:
  DCGM_FI_DEV_GPU_TEMP: 86
  DCGM_FI_DEV_POWER_USAGE: 612
  DCGM_FI_DEV_POWER_MGMT_LIMIT: 700

normalized:
  gpu:
    temperature_c: 86
    power_usage_watts: 612
    power_limit_watts: 700

derived:
  power_headroom_watts: 88
  power_utilization_ratio: 0.874
  thermal_state: warning
```

## Components

```text
FastAPI backend
  - API routes
  - API-key authentication and role authorization
  - simulator clock
  - policy engine
  - recommendation service
  - approval executor
  - post-action verification

SQLite repository
  - simulator snapshots
  - recommendation lifecycle
  - idempotent action executions
  - append-only, retained audit events

Cluster simulator
  - racks
  - nodes
  - GPUs
  - Kubernetes-style pods/workloads
  - pending queue
  - allocation
  - grid-event lifecycle
  - facility power model

Grid control
  - normalized GridEvent ingestion
  - rack flexibility offers
  - safety-first ActionArbiter
  - GPU power-limit planning
  - target compliance tracking
  - ramped recovery

Telemetry exporter
  - /metrics DCGM Exporter-compatible view
  - /metrics/simulator native project metrics
  - /metrics/dcgm backward-compatible alias

Prometheus and Grafana
  - real observability pipeline over synthetic signals
```

## Parallel Control Paths

```text
DCGM / Kubernetes / Redfish telemetry       GridEvent API
                 |                              |
                 v                              v
      operational safety policy        flexibility planner
                 |                              |
                 +----------+-------------------+
                            v
                       ActionArbiter
                 safety > SLA > grid target
                            |
                            v
       checkpoint / evict / requeue / GPU power cap / restore
                            |
                            v
                   verification and audit
```

The grid path never bypasses the existing safety path. A grid event is rejected
when cooling headroom is below 5% or a GPU has reached the configured thermal
slowdown threshold. Only one active or recovering grid event may own a rack.

Grid events use the lifecycle:

```text
active | under_delivering -> recovery -> closed
rejected
failed
```

An event can enter recovery when its requested duration expires or when an
authorized caller completes it. Recovery restores GPU limits at a bounded
rack-level ramp rate to reduce rebound demand.

## Time Behavior

Recommended update frequencies:

| Source | Frequency |
| --- | ---: |
| GPU utilization, power, temperature | 1 second |
| GPU memory and clock | 1-5 seconds |
| ECC and XID events | event-driven |
| Kubernetes Pod and Node state | 2-5 seconds |
| workload progress accounting | 5-30 seconds |
| node inventory | 30-60 seconds |
| Redfish-style temperature and power | 5-15 seconds |

The MVP currently uses a 1-second simulator tick and can later add stale samples, scrape jitter, exporter restarts, and counter resets.

The simulator and policy monitor each run in one background thread inside one
API process. The supported deployment topology is therefore one backend
process. A future horizontally scaled version should move these loops behind
leader election or into a dedicated worker.

## Safety Model

```text
authenticated admin
  -> durable recommendation lookup
  -> fresh policy re-evaluation
  -> atomic idempotency claim
  -> action precondition validation
  -> workload-scoped resource release
  -> post-action invariant verification
  -> durable action result and audit event
```

Recommendations include a policy version, snapshot hash, and expiry. Approval
never trusts the original recommendation alone; current cluster state and
guardrails are evaluated again before execution.

The thermal policy requires full-node evacuation. Nodes with colocated
workloads that cannot all be moved safely are reported as blocked; the engine
prefers another actionable node instead of reporting a partial evacuation as a
success.

Grid events are pre-authorized control requests and therefore require the admin
role at ingestion. Every applied GPU limit and lifecycle transition is audited.
Provider-specific authentication, signal verification, replay protection, and
market settlement are outside this first normalized API slice.

## Kubernetes Control Model

The simulator treats Kubernetes as the primary control plane contract:

- GPU capacity is represented as `nvidia.com/gpu` on Node `status.capacity` and `status.allocatable`.
- Workloads are represented as Pod-shaped records with `metadata`, `spec`, and `status`.
- Policy metadata is represented as Pod annotations such as `policy.gpu-ops/checkpointable`.
- A controlled operation is modeled as `checkpoint_workload -> cordon_node -> evict_pod -> requeue_workload`.
- Restoring a node is modeled as removing simulator taints and clearing `spec.unschedulable`.

Slurm can be added later as a second adapter, but it is no longer the default MVP contract.
