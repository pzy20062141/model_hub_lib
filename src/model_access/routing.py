from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .contracts.entities import SelfHostedDeployment, SelfHostedEndpoint
from .contracts.enums import ErrorCode
from .errors import ModelAccessException


@dataclass(slots=True)
class EndpointState:
    current_weight: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0


class SelfHostedEndpointRouter:
    """Thread-safe smooth weighted round robin with bounded endpoint cooldown."""

    def __init__(self, *, failure_threshold: int = 2, cooldown_seconds: float = 10.0):
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_seconds = max(0.1, cooldown_seconds)
        self._states: dict[str, EndpointState] = {}
        self._lock = threading.Lock()

    def select(self, deployment: SelfHostedDeployment) -> SelfHostedEndpoint:
        now = time.monotonic()
        enabled = [item for item in deployment.endpoints if item.enabled]
        if not enabled:
            raise ModelAccessException(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "self-hosted deployment has no enabled endpoint",
                retryable=True,
            )
        with self._lock:
            healthy = [
                item
                for item in enabled
                if self._states.setdefault(item.endpoint_id, EndpointState()).cooldown_until <= now
            ]
            candidates = (
                healthy
                or sorted(
                    enabled,
                    key=lambda item: self._states[item.endpoint_id].cooldown_until,
                )[:1]
            )
            total_weight = sum(item.weight for item in candidates)
            selected: SelfHostedEndpoint | None = None
            selected_state: EndpointState | None = None
            for item in candidates:
                state = self._states[item.endpoint_id]
                state.current_weight += item.weight
                if selected_state is None or state.current_weight > selected_state.current_weight:
                    selected = item
                    selected_state = state
            assert selected is not None and selected_state is not None
            selected_state.current_weight -= total_weight
            return selected

    def report_success(self, endpoint_id: str) -> None:
        with self._lock:
            state = self._states.setdefault(endpoint_id, EndpointState())
            state.consecutive_failures = 0
            state.cooldown_until = 0.0

    def report_failure(self, endpoint_id: str) -> None:
        with self._lock:
            state = self._states.setdefault(endpoint_id, EndpointState())
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._failure_threshold:
                state.cooldown_until = time.monotonic() + self._cooldown_seconds
                state.consecutive_failures = 0
