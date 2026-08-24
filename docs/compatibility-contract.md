# Compatibility Contract

This project generates synthetic values behind versioned, production-shaped
interfaces. Compatibility means that consumers can parse and query the
supported surface without simulator-specific transformations; it does not mean
that every NVIDIA or Kubernetes feature is implemented.

## Pinned Baselines

| Interface | Baseline | Supported surface |
| --- | --- | --- |
| NVIDIA DCGM Exporter | `4.6.0-4.8.3` naming and label conventions | selected full-GPU metrics, Kubernetes workload labels, Prometheus text format |
| Kubernetes | `v1.36.2`, core `v1` | discovery, Pod get/list/create subset, Node get/list |
| Prometheus | text exposition `0.0.4` | scrape-compatible `HELP`, `TYPE`, labels, samples |

The DCGM baseline uses `hostname`, the label name introduced by current DCGM
Exporter releases, and always emits `gpu`, `UUID`, `pci_bus_id`, `device`, and
`modelName`. `pod`, `namespace`, and `container` are added when a simulated GPU
is allocated to a Pod.

## Endpoints

### DCGM and simulator telemetry

| Endpoint | Contract |
| --- | --- |
| `GET /metrics` | DCGM Exporter-compatible metrics for Prometheus |
| `GET /metrics/dcgm` | backward-compatible alias of `/metrics` |
| `GET /metrics/simulator` | project-native rack, policy, and grid metrics |

The simulated exporter represents a cluster-wide aggregation. It preserves a
`hostname` label for node identity instead of running one HTTP server per node.
Prometheus can query it like a set of per-node exporters, but target-level
labels such as `instance` describe the simulator backend.

### Kubernetes core API facade

| Endpoint | Operation |
| --- | --- |
| `GET /version` | pinned simulated server version |
| `GET /api` | core API version discovery |
| `GET /apis` | API group discovery |
| `GET /api/v1` | Pod and Node resource discovery |
| `GET /api/v1/pods` | all-namespace `PodList` |
| `GET /api/v1/namespaces/{namespace}/pods` | namespaced `PodList` |
| `GET /api/v1/namespaces/{namespace}/pods/{name}` | core/v1 `Pod` |
| `POST /api/v1/namespaces/{namespace}/pods` | create a simulated GPU Pod |
| `GET /api/v1/nodes` | core/v1 `NodeList` |
| `GET /api/v1/nodes/{name}` | core/v1 `Node` |

Kubernetes failures use core/v1 `Status` objects with `NotFound`,
`AlreadyExists`, or `BadRequest` reasons. Resource quantities, including
`nvidia.com/gpu`, are serialized as Kubernetes quantity strings.

The Pod create subset requires:

- `apiVersion: v1` and `kind: Pod`
- `metadata.name`
- exactly one container (the supported simulator subset)
- an integer `nvidia.com/gpu` limit; a request, when present, must equal it

Simulator policy inputs remain ordinary Kubernetes annotations. The optional
`simulator.gpu-ops/duration-seconds` annotation controls synthetic runtime.

## Semantic Boundaries

- Node `status.capacity` and `status.allocatable` follow Kubernetes semantics.
  They are not decremented as Pods bind. The simulator's private cluster
  snapshot retains a separate free-resource view for its scheduler.
- Pod `status` excludes simulator-only progress fields. Full progress remains
  available from `/api/v1/workloads`.
- DCGM values use realistic units and temporal correlations but are not a
  hardware-accuracy model for a specific DGX, HGX, or NVL rack.
- MIG, DRA, NvSwitch, NvLink topology, API watch streams, server-side apply,
  admission, and the complete Kubernetes OpenAPI surface are not implemented.

## Contract Verification

`tests/test_compatibility_contracts.py` locks down:

- DCGM metric names, types, metadata, identity labels, and Pod mapping labels
- separation between `/metrics` and `/metrics/simulator`
- core/v1 Pod, Node, PodList, NodeList, and Status envelopes
- Kubernetes resource-quantity strings and Node allocatable semantics
- Pod creation and duplicate/not-found behavior

Run the contract and regression suite with:

```bash
make test
```

When `promtool` is installed, validate a live scrape with:

```bash
curl -fsS http://localhost:8000/metrics | promtool check metrics
```

`promtool` accepts the exposition syntax but reports naming lint for upstream
DCGM identifiers such as `modelName` and counters whose official names do not
end in `_total`. Those warnings are expected: renaming them would break the
DCGM Exporter contract. The automated suite checks the pinned upstream names
and labels directly.
