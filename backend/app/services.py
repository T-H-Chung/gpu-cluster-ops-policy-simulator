from __future__ import annotations

import re
import threading
from pathlib import Path
from time import sleep

import yaml

from app.policy.engine import PolicyEngine
from app.repository import SQLiteRepository
from app.simulator.cluster import ClusterSimulator


class RecommendationService:
    def __init__(
        self,
        simulator: ClusterSimulator,
        policy_engine: PolicyEngine,
        repository: SQLiteRepository,
    ) -> None:
        self.simulator = simulator
        self.policy_engine = policy_engine
        self.repository = repository
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.refresh()
            except Exception as error:
                self.repository.append_audit(
                    "policy_evaluation_failed",
                    {"error": type(error).__name__, "message": str(error)},
                    "system",
                    "",
                )
            sleep(1)

    def refresh(self) -> list[dict]:
        with self._lock:
            evaluated = self.policy_engine.evaluate(self.simulator.snapshot())
            self.repository.sync_recommendations(evaluated)
            return self.repository.list_active_recommendations()

    def approve(
        self,
        *,
        recommendation_id: str,
        idempotency_key: str,
        actor: str,
        request_id: str,
        reason: str,
    ) -> dict:
        with self._lock:
            previous = self.repository.get_action(idempotency_key)
            if previous is not None:
                if previous["recommendation_id"] != recommendation_id:
                    return {
                        "status": "conflict",
                        "reason": "idempotency key was used for another recommendation",
                    }
                return previous["result"] or {
                    "status": previous["status"],
                    "recommendation_id": recommendation_id,
                }

            current = self.policy_engine.evaluate(self.simulator.snapshot())
            self.repository.sync_recommendations(current)
            recommendation = next(
                (item for item in current if item["id"] == recommendation_id),
                None,
            )
            if recommendation is None:
                return {
                    "status": "stale",
                    "reason": "recommendation is no longer applicable",
                }
            if recommendation["status"] != "pending_approval":
                return {
                    "status": "blocked",
                    "reason": "recommendation cannot be approved",
                }

            claim = self.repository.claim_action(
                recommendation_id=recommendation_id,
                idempotency_key=idempotency_key,
                actor=actor,
                request_id=request_id,
                reason=reason,
            )
            if not claim["claimed"]:
                if claim.get("duplicate"):
                    return claim.get("result") or {
                        "status": claim["status"],
                        "recommendation_id": recommendation_id,
                    }
                return {"status": "conflict", "reason": claim["reason"]}

            try:
                result = self.simulator.execute_recommendation(
                    recommendation,
                    actor=actor,
                    request_id=request_id,
                )
            except Exception as error:
                result = {
                    "status": "failed",
                    "reason": f"{type(error).__name__}: {error}",
                    "recommendation_id": recommendation_id,
                }
            self.repository.complete_action(
                recommendation_id=recommendation_id,
                idempotency_key=idempotency_key,
                result=result,
            )
            self.repository.append_audit(
                "approve_recommendation",
                {
                    "recommendation_id": recommendation_id,
                    "reason": reason,
                    "result": result["status"],
                },
                actor,
                request_id,
            )
            if result["status"] == "executed":
                self.policy_engine.start_cooldown()
            return result


class ScenarioService:
    def __init__(self, scenario_directory: Path, simulator: ClusterSimulator) -> None:
        self.scenario_directory = scenario_directory
        self.simulator = simulator

    def inject(self, name: str, *, actor: str, request_id: str) -> dict:
        if name == "restore-normal":
            self.simulator.restore_normal(actor=actor, request_id=request_id)
            return {"scenario": name, "status": "restored"}
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
            raise KeyError(name)
        path = self.scenario_directory / f"{name}.yaml"
        if not path.is_file():
            raise KeyError(name)
        with path.open("r", encoding="utf-8") as file:
            scenario = yaml.safe_load(file)
        applied = 0
        for step in scenario.get("steps", []):
            if step.get("at") not in {0, "0s"}:
                continue
            if step.get("action") == "set_cooling_efficiency":
                self.simulator.degrade_rack_cooling(
                    step["rack"],
                    float(step["value"]),
                    actor=actor,
                    request_id=request_id,
                )
                applied += 1
            elif "action" in step:
                raise ValueError(f"unsupported scenario action: {step['action']}")
        if applied == 0:
            raise ValueError("scenario has no immediately executable steps")
        return {"scenario": name, "status": "injected", "applied_steps": applied}
