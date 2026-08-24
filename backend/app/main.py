from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.auth import Authenticator, Principal
from app.config import Settings
from app.kubernetes import (
    api_resource_list,
    node_resource,
    pod_create_to_workload,
    pod_resource,
    resource_list,
    status_failure,
)
from app.policy.engine import PolicyEngine
from app.repository import SQLiteRepository
from app.schemas import (
    ApprovalRequest,
    GridEventCompleteRequest,
    GridEventCreateRequest,
    WorkloadCreateRequest,
)
from app.services import RecommendationService, ScenarioService
from app.simulator.cluster import ClusterSimulator


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "policies" / "rack-thermal-protection.yaml"
SCENARIO_DIRECTORY = ROOT / "scenarios"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env(ROOT / "data" / "simulator.db")
    repository = SQLiteRepository(resolved_settings.database_path)
    interrupted_recommendations = repository.recover_interrupted_actions()
    for recommendation_id in interrupted_recommendations:
        repository.append_audit(
            "action_recovery_required",
            {"recommendation_id": recommendation_id},
            "system",
            "",
        )
    repository.prune_audit(
        retention_days=resolved_settings.audit_retention_days,
        max_rows=resolved_settings.audit_max_rows,
    )
    simulator = ClusterSimulator(
        seed=resolved_settings.simulator_seed,
        audit_sink=repository.append_audit,
        state_sink=repository.save_state,
    )
    persisted_state = repository.load_state()
    if persisted_state:
        simulator.restore_state(persisted_state)
    policy_engine = PolicyEngine.from_yaml(POLICY_PATH)
    recommendation_service = RecommendationService(
        simulator,
        policy_engine,
        repository,
    )
    scenario_service = ScenarioService(SCENARIO_DIRECTORY, simulator)
    authenticator = Authenticator(resolved_settings)

    viewer = authenticator.require("viewer")
    operator = authenticator.require("operator")
    admin = authenticator.require("admin")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        simulator.start()
        recommendation_service.start()
        try:
            yield
        finally:
            recommendation_service.stop()
            simulator.stop()

    application = FastAPI(
        title="GPU Cluster Operations Policy Simulator",
        version="0.3.0",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.repository = repository
    application.state.simulator = simulator
    application.state.policy_engine = policy_engine
    application.state.recommendation_service = recommendation_service

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if supplied_request_id and len(supplied_request_id) <= 128
            else str(uuid4())
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/livez")
    def livez() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/readyz")
    def readyz() -> dict[str, str]:
        if (
            not repository.ping()
            or not simulator.is_running
            or not recommendation_service.is_running
        ):
            raise HTTPException(status_code=503, detail="service is not ready")
        return {"status": "ready"}

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/version")
    def kubernetes_version(_: Principal = Depends(viewer)) -> dict[str, str]:
        return {
            "major": "1",
            "minor": "36",
            "gitVersion": "v1.36.2-simulator",
            "gitCommit": "simulated",
            "gitTreeState": "clean",
            "buildDate": "2026-06-09T00:00:00Z",
            "goVersion": "go1.22.0",
            "compiler": "gc",
            "platform": "linux/amd64",
        }

    @application.get("/api")
    def kubernetes_api_versions(_: Principal = Depends(viewer)) -> dict:
        return {
            "kind": "APIVersions",
            "apiVersion": "v1",
            "versions": ["v1"],
            "serverAddressByClientCIDRs": [],
        }

    @application.get("/apis")
    def kubernetes_api_groups(_: Principal = Depends(viewer)) -> dict:
        return {
            "kind": "APIGroupList",
            "apiVersion": "v1",
            "groups": [],
        }

    @application.get("/api/v1")
    def kubernetes_core_resources(_: Principal = Depends(viewer)) -> dict:
        return api_resource_list()

    def kubernetes_pods(namespace: str | None = None) -> list[dict]:
        pods = [pod_resource(workload) for workload in simulator.list_jobs()]
        if namespace is not None:
            pods = [
                pod for pod in pods if pod["metadata"]["namespace"] == namespace
            ]
        return pods

    @application.get("/api/v1/pods")
    def list_all_pods(_: Principal = Depends(viewer)) -> dict:
        return resource_list("Pod", kubernetes_pods())

    @application.get("/api/v1/namespaces/{namespace}/pods")
    def list_namespaced_pods(
        namespace: str,
        _: Principal = Depends(viewer),
    ) -> dict:
        return resource_list("Pod", kubernetes_pods(namespace))

    @application.get("/api/v1/namespaces/{namespace}/pods/{pod_name}")
    def read_namespaced_pod(
        namespace: str,
        pod_name: str,
        _: Principal = Depends(viewer),
    ) -> Any:
        pod = next(
            (
                item
                for item in kubernetes_pods(namespace)
                if item["metadata"]["name"] == pod_name
            ),
            None,
        )
        if pod is not None:
            return pod
        return JSONResponse(
            status_code=404,
            content=status_failure(
                f'pods "{pod_name}" not found', reason="NotFound", code=404
            ),
        )

    @application.post("/api/v1/namespaces/{namespace}/pods", status_code=201)
    def create_namespaced_pod(
        namespace: str,
        request: Request,
        pod: dict = Body(...),
        principal: Principal = Depends(operator),
    ) -> Any:
        try:
            workload_payload = WorkloadCreateRequest.model_validate(
                pod_create_to_workload(pod, namespace)
            ).model_dump(exclude_none=True)
        except ValueError as error:
            return JSONResponse(
                status_code=400,
                content=status_failure(str(error), reason="BadRequest", code=400),
            )
        pod_name = workload_payload["pod_name"]
        if any(
            item["metadata"]["name"] == pod_name
            for item in kubernetes_pods(namespace)
        ):
            return JSONResponse(
                status_code=409,
                content=status_failure(
                    f'pods "{pod_name}" already exists',
                    reason="AlreadyExists",
                    code=409,
                ),
            )
        workload = simulator.submit_job(
            workload_payload,
            actor=principal.actor,
            request_id=request.state.request_id,
        )
        return pod_resource(workload)

    @application.get("/api/v1/nodes")
    def list_nodes(_: Principal = Depends(viewer)) -> dict:
        snapshot = simulator.snapshot()
        return resource_list(
            "Node",
            [node_resource(node) for node in snapshot["nodes"].values()],
        )

    @application.get("/api/v1/nodes/{node_name}")
    def read_node(
        node_name: str,
        _: Principal = Depends(viewer),
    ) -> Any:
        node = simulator.snapshot()["nodes"].get(node_name)
        if node is not None:
            return node_resource(node)
        return JSONResponse(
            status_code=404,
            content=status_failure(
                f'nodes "{node_name}" not found', reason="NotFound", code=404
            ),
        )

    @application.get("/api/v1/cluster")
    @application.get("/api/cluster", include_in_schema=False)
    def cluster(_: Principal = Depends(viewer)) -> dict:
        return simulator.snapshot()

    @application.get("/api/v1/workloads")
    @application.get("/api/workloads", include_in_schema=False)
    @application.get("/api/jobs", include_in_schema=False)
    def workloads(_: Principal = Depends(viewer)) -> list[dict]:
        return simulator.list_jobs()

    @application.get("/api/v1/flexibility")
    def flexibility(
        rack_id: str = Query(default="rack-a", min_length=1, max_length=128),
        _: Principal = Depends(viewer),
    ) -> dict:
        try:
            return simulator.flexibility_offer(rack_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown rack: {rack_id}",
            ) from None

    @application.get("/api/v1/grid-events")
    def grid_events(_: Principal = Depends(viewer)) -> list[dict]:
        return simulator.list_grid_events()

    @application.post("/api/v1/grid-events", status_code=201)
    def create_grid_event(
        grid_event: GridEventCreateRequest,
        request: Request,
        principal: Principal = Depends(admin),
    ) -> dict:
        try:
            return simulator.create_grid_event(
                grid_event.model_dump(exclude_none=True),
                actor=principal.actor,
                request_id=request.state.request_id,
            )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown rack: {grid_event.rack_id}",
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/v1/grid-events/{event_id}/complete")
    def complete_grid_event(
        event_id: str,
        completion: GridEventCompleteRequest,
        request: Request,
        principal: Principal = Depends(admin),
    ) -> dict:
        try:
            return simulator.complete_grid_event(
                event_id,
                reason=completion.reason,
                actor=principal.actor,
                request_id=request.state.request_id,
            )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown grid event: {event_id}",
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/audit")
    @application.get("/api/audit", include_in_schema=False)
    def audit(
        limit: int = Query(default=100, ge=1, le=500),
        before_id: int | None = Query(default=None, ge=1),
        _: Principal = Depends(viewer),
    ) -> list[dict]:
        return repository.list_audit(limit=limit, before_id=before_id)

    @application.post("/api/v1/workloads", status_code=201)
    @application.post("/api/workloads", include_in_schema=False, status_code=201)
    @application.post("/api/jobs", include_in_schema=False, status_code=201)
    def submit_workload(
        workload: WorkloadCreateRequest,
        request: Request,
        principal: Principal = Depends(operator),
    ) -> dict:
        return simulator.submit_job(
            workload.model_dump(exclude_none=True),
            actor=principal.actor,
            request_id=request.state.request_id,
        )

    @application.post("/api/v1/scenarios/{scenario_name}")
    @application.post("/api/scenarios/{scenario_name}", include_in_schema=False)
    def inject_scenario(
        scenario_name: str,
        request: Request,
        principal: Principal = Depends(operator),
    ) -> dict:
        try:
            return scenario_service.inject(
                scenario_name,
                actor=principal.actor,
                request_id=request.state.request_id,
            )
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"unknown scenario: {scenario_name}",
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.get("/api/v1/recommendations")
    @application.get("/api/recommendations", include_in_schema=False)
    def recommendations(_: Principal = Depends(viewer)) -> list[dict]:
        return recommendation_service.refresh()

    @application.post("/api/v1/recommendations/{recommendation_id}/approve")
    @application.post(
        "/api/recommendations/{recommendation_id}/approve",
        include_in_schema=False,
    )
    def approve_recommendation(
        recommendation_id: str,
        approval: ApprovalRequest,
        request: Request,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=128,
        ),
        principal: Principal = Depends(admin),
    ) -> dict:
        key = idempotency_key or f"approve:{recommendation_id}"
        result = recommendation_service.approve(
            recommendation_id=recommendation_id,
            idempotency_key=key,
            actor=principal.actor,
            request_id=request.state.request_id,
            reason=approval.reason,
        )
        if result["status"] in {"stale", "conflict"}:
            raise HTTPException(status_code=409, detail=result)
        if result["status"] == "blocked":
            raise HTTPException(status_code=422, detail=result)
        if result["status"] == "failed":
            raise HTTPException(status_code=500, detail=result)
        return result

    @application.get("/metrics")
    @application.get("/metrics/dcgm", include_in_schema=False)
    def dcgm_metrics() -> Response:
        return Response(
            simulator.render_dcgm_metrics(),
            media_type="text/plain; version=0.0.4",
        )

    @application.get("/metrics/simulator")
    def native_metrics() -> Response:
        return Response(
            simulator.render_native_metrics(),
            media_type="text/plain; version=0.0.4",
        )

    return application
