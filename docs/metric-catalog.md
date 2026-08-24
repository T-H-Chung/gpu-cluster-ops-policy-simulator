# Metric Catalog

This catalog defines the first credible metric set. Values are synthetic, but names, units, labels, type choices, and relationships are modeled after real GPU cluster operations. The DCGM interface is pinned to the compatibility surface described in [`compatibility-contract.md`](compatibility-contract.md).

## Simulation Assumptions

```yaml
gpu_profile:
  model: simulated-hopper-class
  framebuffer_total_mib: 81920
  power_limit_default_watts: 700
  power_limit_min_watts: 200
  power_limit_max_watts: 700
  slowdown_temperature_c: 87
  shutdown_temperature_c: 92
  idle_power_watts: 65
```

These are project assumptions, not universal NVIDIA hardware thresholds.

## GPU Metrics

| Metric | Type | Unit | Labels | Normal range | Fault behavior |
| --- | --- | --- | --- | --- | --- |
| `DCGM_FI_DEV_GPU_TEMP` | gauge | Celsius | `gpu`, `UUID`, `pci_bus_id`, `device`, `modelName`, `hostname`; Pod labels when allocated | idle 30-45, training 55-82 | rises gradually during cooling failure |
| `DCGM_FI_DEV_MEMORY_TEMP` | gauge | Celsius | same | GPU temp + 2-6 | rises with GPU temp |
| `DCGM_FI_DEV_POWER_USAGE` | gauge | watts | same | idle 50-100, training 350-650 | capped by power limit |
| `DCGM_FI_DEV_POWER_MGMT_LIMIT` | gauge | watts | same | 700 | power cap scenario lowers it |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | counter | millijoules | same | monotonically increasing | may reset on exporter/device reset scenario |
| `DCGM_FI_DEV_GPU_UTIL` | gauge | percent | same | idle 0-5, training 70-99 | can remain high during throttling |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | gauge | percent | same | idle 0-3, training 25-70 | workload dependent |
| `DCGM_FI_DEV_FB_USED` | gauge | MiB | same | 0-73728 | allocated by Kubernetes-style scheduler simulation |
| `DCGM_FI_DEV_SM_CLOCK` | gauge | MHz | same | busy 1600-1800 | drops under thermal or power throttle |
| `DCGM_FI_DEV_XID_ERRORS` | gauge | event value | same | 0 | set by injected GPU fault |
| `DCGM_FI_DEV_ECC_SBE_AGG_TOTAL` | counter | errors | same | usually unchanged | increments on ECC scenario |
| `DCGM_FI_DEV_ECC_DBE_AGG_TOTAL` | counter | errors | same | usually unchanged | increments on severe ECC scenario |

## Native Derived Metrics

| Metric | Type | Unit | Labels | Definition |
| --- | --- | --- | --- | --- |
| `gpu_simulator_gpu_temperature_celsius` | gauge | Celsius | `hostname`, `gpu`, `uuid`, `model` | native view of GPU temp |
| `gpu_simulator_gpu_power_usage_watts` | gauge | watts | same | native view of power |
| `gpu_simulator_node_max_gpu_temperature_celsius` | gauge | Celsius | `hostname` | max GPU temp per node |
| `gpu_simulator_rack_cooling_headroom_pct` | gauge | percent | `rack` | `(effective_cooling - heat_load) / effective_cooling * 100` |
| `gpu_simulator_rack_facility_power_watts` | gauge | watts | `rack` | simulated utility-meter power: GPU + non-GPU IT + cooling |
| `gpu_simulator_grid_event_target_reduction_watts` | gauge | watts | `event_id`, `rack`, `status` | requested reduction from the event baseline |
| `gpu_simulator_grid_event_achieved_reduction_watts` | gauge | watts | same | baseline facility power minus current facility power |
| `gpu_simulator_grid_event_compliance_ratio` | gauge | ratio | same | achieved reduction divided by requested reduction, capped at 1 |

## Kubernetes-Style Workload Fields

| Field | Example | Source layer |
| --- | --- | --- |
| `metadata.name` | `llama-training-pod` | Kubernetes Pod |
| `metadata.namespace` | `ml-workloads` | Kubernetes Pod |
| `metadata.uid` | `pod-1042` | Kubernetes Pod |
| `metadata.ownerReferences[0].kind` | `Job` | Kubernetes owner |
| `metadata.ownerReferences[0].name` | `llama-training` | Kubernetes owner |
| `spec.nodeName` | `gpu-node-01` | scheduler binding |
| `spec.priority` | `128432` | Kubernetes scheduler |
| `spec.priorityClassName` | `batch-normal` | Kubernetes scheduler |
| `spec.containers[].resources.requests.nvidia.com/gpu` | `2` | Pod resource request |
| `status.phase` | `Running` | Kubernetes Pod status |
| `status.reason` | `Resources` | Kubernetes Pod status |
| `status.elapsedSeconds` | `1840` | simulator accounting |

## Kubernetes-Style Node Fields

| Field | Example | Source layer |
| --- | --- | --- |
| `metadata.name` | `gpu-node-01` | Kubernetes Node |
| `metadata.labels.topology.kubernetes.io/rack` | `rack-a` | topology labels |
| `spec.unschedulable` | `false` | cordon state |
| `spec.taints` | `policy.gpu-ops/thermal-protection=true:NoSchedule` | policy action |
| `status.conditions[type=Ready].status` | `True` | Node health |
| `status.capacity.nvidia.com/gpu` | `2` | device plugin style resource |
| `status.allocatable.nvidia.com/gpu` | `1` | scheduler-visible GPU capacity |

## Workload Policy Metadata

These are not modeled as native hardware metrics. They represent workload catalog or Kubernetes Pod metadata merged into normalized state.

| Field | Example |
| --- | --- |
| `sla_class` | `batch`, `production` |
| `checkpointable` | `true` |
| `checkpoint_interval_seconds` | `300` |
| `preemptible` | `true` |
| `flex_class` | `flex_0`, `flex_1`, `flex_2`, or `flex_3` |
| `max_throughput_reduction_pct` | `50` |
| `deadline_slack_seconds` | `3600` |
| `geo_shiftable` | `false` |

In the K8s-shaped API they are exposed as annotations:

| Annotation | Example |
| --- | --- |
| `policy.gpu-ops/sla-class` | `batch` |
| `policy.gpu-ops/checkpointable` | `true` |
| `policy.gpu-ops/checkpoint-interval-seconds` | `300` |
| `policy.gpu-ops/preemptible` | `true` |
| `policy.gpu-ops/flex-class` | `flex_2` |
| `policy.gpu-ops/max-throughput-reduction-pct` | `50.0` |
| `policy.gpu-ops/deadline-slack-seconds` | `3600` |
| `policy.gpu-ops/geo-shiftable` | `false` |

## Redfish-Style Facility Fields

| Field | Type | Unit | Scope |
| --- | --- | --- | --- |
| `environment_metrics.power_watts.reading` | gauge | watts | rack |
| `environment_metrics.energy_joules.reading` | counter | joules | rack |
| `environment_metrics.inlet_temperature_celsius.reading` | gauge | Celsius | rack |
| `environment_metrics.exhaust_temperature_celsius.reading` | gauge | Celsius | rack |
| `environment_metrics.fan_speed_percent.reading` | gauge | percent | rack |
| `power_capacity_watts` | gauge | watts | rack |
| `health` | enum | n/a | rack |

## Correlation Rules

- Cooling degradation lowers `cooling_efficiency`.
- Lower cooling efficiency raises rack inlet temperature gradually.
- Higher inlet temperature raises GPU and memory temperatures gradually.
- Reaching slowdown temperature sets `thermal_slowdown`.
- Thermal slowdown lowers SM clock.
- Lower SM clock increases estimated job runtime even if GPU utilization remains high.
- Power caps lower `DCGM_FI_DEV_POWER_MGMT_LIMIT`, cap power usage, and can lower SM clock while utilization stays high.
- Grid-event planning excludes `flex_0`, production, and non-preemptible workloads.
- `flex_1` workloads retain at least 75% of current GPU power; `flex_2` and
  `flex_3` workloads may be capped to the configured GPU minimum in this release.
- Facility power includes synthetic non-GPU IT and cooling components, so a
  change in GPU power is not reported as an identical utility-meter change.
