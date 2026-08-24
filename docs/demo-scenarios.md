# Demo Scenarios

## Scenario 1: Rack A Cooling Degradation

Target duration: 5-7 minutes.

1. Show normal state.
2. Inject `rack-a-cooling-degradation`.
3. Watch rack cooling headroom fall.
4. Watch GPU temperatures rise gradually.
5. Policy emits a recommendation.
6. Approve the recommendation.
7. Simulator checkpoints the workload, cordons the hot node, evicts the pod, requeues the workload, and records audit events.
8. Restore normal state.

API sequence:

```bash
curl http://localhost:8000/api/v1/cluster
curl http://localhost:8000/api/v1/workloads
curl -X POST http://localhost:8000/api/v1/scenarios/rack-a-cooling-degradation
curl http://localhost:8000/api/v1/recommendations

# Use the current recommendation id returned above. It includes the policy version.
curl -X POST \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-rack-a-1' \
  -d '{"reason":"Demo approval after reviewing thermal evidence"}' \
  http://localhost:8000/api/v1/recommendations/RECOMMENDATION_ID/approve

curl -X POST http://localhost:8000/api/v1/scenarios/restore-normal
curl http://localhost:8000/api/v1/audit
```

When authentication is enabled, add the appropriate bearer token to each
request. Viewer access is sufficient for reads, operator access is required for
scenario changes, and admin access is required for approval.

## Scenario 2: Guardrail Block

Place a production, non-checkpointable pod on a hot node. The policy should produce a blocked recommendation:

```text
Automatic eviction blocked. Node has no checkpointable non-production pod eligible for automated action.
```

This proves the simulator is not blindly acting on alerts.

## Scenario 3: 30% Grid Emergency Curtailment

Target duration: 3-5 minutes in the accelerated simulator.

1. Read the current rack flexibility offer.
2. Submit a 30% reduction target with a 40-second response deadline.
3. Verify that production inference remains at its original power limit.
4. Observe lower limits on flexible GPUs and event compliance metrics.
5. Complete the event.
6. Observe ramped restoration to the original power limits.

```bash
curl http://localhost:8000/api/v1/flexibility?rack_id=rack-a

curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "grid-demo-1",
    "source": "demo-grid",
    "event_type": "emergency_curtailment",
    "rack_id": "rack-a",
    "requested_reduction_pct": 30,
    "response_deadline_seconds": 40,
    "duration_seconds": 7200,
    "recovery_ramp_watts_per_second": 100
  }' \
  http://localhost:8000/api/v1/grid-events

curl http://localhost:8000/api/v1/grid-events

curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"reason":"Grid released the emergency event"}' \
  http://localhost:8000/api/v1/grid-events/grid-demo-1/complete
```

When authentication is enabled, viewer credentials are sufficient for the two
GET requests and admin credentials are required for both POST requests.
