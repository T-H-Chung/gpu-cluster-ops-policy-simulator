# GPU Cluster Operations Policy Simulator

A reproducible digital simulation of GPU cluster operations: synthetic telemetry, workload scheduling, thermal and power faults, grid events, policy evaluation, explainable recommendations, approval workflow, controlled actions, and audit logging.

The project emits synthetic telemetry modeled after NVIDIA DCGM Exporter, Kubernetes GPU workload records, Prometheus conventions, and Redfish-style environmental resources. It does not claim hardware-level physical accuracy. Its purpose is to reproduce realistic metric semantics, temporal behavior, cross-layer correlations, failure scenarios, and operational decision workflows.

## Why This Project Matters

GPU clusters are increasingly constrained by more than compute capacity. In real operations, workload scheduling, GPU telemetry, cooling headroom, power limits, grid events, and human approval all affect whether an action is safe. A useful operations system therefore needs to do more than display metrics: it must translate telemetry into decisions, explain those decisions, enforce guardrails, and preserve an audit trail.

This project demonstrates that operational layer in a reproducible simulator. It models DCGM-style GPU metrics, Kubernetes-style workloads, Redfish-style facility signals, policy-based recommendations, approval workflows, guarded actions, and post-action verification. The goal is not hardware-level physical accuracy, but realistic operational semantics: how infrastructure state becomes an explainable and auditable control decision.

By combining observability, policy evaluation, workload control, power management, and audit logging, this simulator shows how GPU infrastructure can be managed as a safety-aware decision system rather than a collection of disconnected dashboards and scripts.

## Decision Loop

```text
Synthetic telemetry
  -> normalized cluster state
  -> policy evaluation
  -> explainable recommendation
  -> manual approval
  -> guarded action execution
  -> verification
  -> audit log
```

Grid response runs as a parallel decision path:

```text
Normalized grid event
  -> flexibility offer
  -> safety-first action arbitration
  -> GPU power-limit plan
  -> closed-loop target verification
  -> ramped recovery
  -> audit log
```

Both paths share the same simulator state and actuators. Grid control is
rejected when cooling headroom or GPU temperature places the rack under
operational safety ownership.

## Research Motivation

The grid-responsive power-control path is motivated by the operational
problems explored in the following papers:

- [Turning AI Data Centers into Grid-Interactive Assets: Results from a Field Demonstration in Phoenix, Arizona](https://arxiv.org/abs/2507.00909)
- [Power-Flexible AI Data Centers: A New Paradigm for Grid-Responsive Compute](https://arxiv.org/abs/2606.25098)

The broader control model is an explicit, auditable loop:
`grid event → SLA/flex tier → power model → policy decision → GPU cap/job pause/traffic shift → telemetry feedback → dashboard/audit log`.
This release implements GPU power capping as the grid-response actuator; job
pausing and traffic shifting are extension points for future control policies.

## Scope

- 1 rack
- 4 GPU nodes
- 2 simulated GPUs per node
- 8 total GPUs
- 6 seeded jobs
- 1 thermal degradation scenario
- 1 YAML policy
- checkpoint, cordon, evict, requeue, uncordon actions
- grid flexibility offers, GPU power capping, target tracking, and ramped recovery
- shared action arbitration that gives operational safety priority over grid control
- FastAPI backend
- Prometheus-compatible `/metrics`
- SQLite-backed simulator state, recommendations, actions, and audit events
- policy conditions, operators, constraints, and durations evaluated from YAML
- optional API-key RBAC for viewer, operator, and admin roles
- idempotent, revalidated approval execution

## Quick Start

Local backend:

```bash
cd gpu-cluster-ops-policy-simulator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
make dev
```

Full stack:

```bash
cd gpu-cluster-ops-policy-simulator
cp .env.example .env
# Add high-entropy API keys before enabling authentication.
docker compose up --build
```

API:

- `GET http://localhost:8000/livez`
- `GET http://localhost:8000/readyz`
- `GET http://localhost:8000/api/v1/cluster`
- `GET http://localhost:8000/api/v1/workloads`
- `GET http://localhost:8000/api/v1/pods`
- `GET http://localhost:8000/api/v1/nodes`
- `POST http://localhost:8000/api/v1/namespaces/{namespace}/pods`
- `GET http://localhost:8000/api/v1/flexibility?rack_id=rack-a`
- `GET http://localhost:8000/api/v1/grid-events`
- `POST http://localhost:8000/api/v1/grid-events`
- `POST http://localhost:8000/api/v1/grid-events/{id}/complete`
- `POST http://localhost:8000/api/v1/scenarios/rack-a-cooling-degradation`
- `GET http://localhost:8000/api/v1/recommendations`
- `POST http://localhost:8000/api/v1/recommendations/{id}/approve`
- `GET http://localhost:8000/api/v1/audit?limit=100`
- `GET http://localhost:8000/metrics`
- `GET http://localhost:8000/metrics/simulator`

`/metrics` is the DCGM Exporter-compatible Prometheus endpoint.
`/metrics/dcgm` remains as a legacy alias. The project-native rack, policy, and
grid series are exposed separately from `/metrics/simulator`.

The read and Pod-create subset of the Kubernetes core/v1 facade is documented
in [`docs/compatibility-contract.md`](docs/compatibility-contract.md).

Legacy unversioned API paths remain available but are omitted from the OpenAPI
schema.

Prometheus:

- `http://localhost:9090`

Grafana:

- `http://localhost:3000`
- provisioned dashboard: `GPU Operations / GPU Cluster Operations Overview`

## Project Layout

```text
backend/        FastAPI app, simulator, policy engine
docs/           architecture, metric catalog, and compatibility contract
policies/       YAML policies
scenarios/      reproducible failure scenarios
prometheus/     scrape configuration
tests/          focused simulator and policy tests
```

## Authentication and Authorization

Authentication is disabled by default for local development. Enable it with:

```bash
export GPU_OPS_AUTH_ENABLED=true
export GPU_OPS_API_KEYS='{"viewer":"...","operator":"...","admin":"..."}'
```

Compose binds all published ports to `127.0.0.1` by default. If
`GPU_OPS_BIND_ADDRESS` is changed, enable authentication and place the services
behind a protected ingress.

Permissions:

| Role | Access |
| --- | --- |
| `viewer` | cluster state, workloads, recommendations, and audit events |
| `operator` | viewer access plus workload submission and scenario injection |
| `admin` | operator access plus recommendation approval |

Creating or completing a grid event requires the `admin` role because it
changes GPU power limits. The grid-event API is the normalized ingestion
boundary for a future ERCOT, utility, market-price, or carbon-intensity
adapter; this release does not claim a live grid-provider integration.

Authenticated request:

```bash
curl -H "Authorization: Bearer $GPU_OPS_VIEWER_KEY" \
  http://localhost:8000/api/v1/cluster
```

Every mutating request is recorded with its actor and `X-Request-ID`. Approval
also requires a reason and supports an `Idempotency-Key`:

```bash
curl -X POST \
  -H "Authorization: Bearer $GPU_OPS_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: incident-2026-07-28-rack-a" \
  -d '{"reason":"Protect rack A while facilities investigates cooling"}' \
  http://localhost:8000/api/v1/recommendations/RECOMMENDATION_ID/approve
```

## Persistence and Runtime Model

The default database is `data/simulator.db`. Docker Compose stores it in the
named `simulator_data` volume. Simulator state is restored on restart, and
recommendations, action executions, and audit events are durable.

Audit events default to 90-day/100,000-row retention. Configure these with
`GPU_OPS_AUDIT_RETENTION_DAYS` and `GPU_OPS_AUDIT_MAX_ROWS`.

The simulator intentionally runs as a single process. Do not add Uvicorn
workers: each worker would otherwise own a separate simulator clock. Horizontal
scale requires extracting the simulation loop into a single elected worker or
service.

## Verification

```bash
make test
docker compose config
```

The tests cover resource ownership, policy duration and constraints, policy
schema rejection, strict API inputs, RBAC, durable state, paginated audit
events, action idempotency, and post-action invariants.

## Current Design Boundary

External interfaces mimic real operational contracts:

- DCGM-style GPU telemetry
- Kubernetes-style Pod and Node records
- Redfish-style facility telemetry
- Prometheus metric naming and type conventions

Internal policy abstractions are explicit derived values, such as
`thermal_state`, `rack_power_headroom_watts`, and `telemetry_age_seconds`.

Facility input power is modeled as GPU load, non-GPU IT load, cooling load, and
fixed overhead. The relationship is synthetic and is intended to prevent GPU
power from being mistaken for utility-meter power, not to predict a specific
facility's PUE.
