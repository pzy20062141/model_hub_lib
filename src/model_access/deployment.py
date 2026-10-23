"""Deployment-time rules for fine-tuned adapters (LoRA).

Fine-tuning produces an *adapter* — a small delta that is only meaningful on top
of its base model. Whether a serving deployment may consume an adapter directly
is not a UI preference: it depends on what the inference engine can do.

Two strategies exist in practice:

``mount``
    Hand the adapter ids to the engine and let it hot-load them (vLLM
    multi-LoRA). Cheap, but the engine must be able to register LoRA ops.

``merge-only``
    Refuse adapters on the deploy path and require the caller to merge the
    adapter into a standalone model artifact first, then deploy *that*. Needed
    on engines that abort while registering LoRA ops — the failure otherwise
    surfaces minutes later, deep in the engine log, far from the user's click.

The checks live here rather than in an individual platform so every consumer of
this library applies the same rule and emits the same actionable message.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

ADAPTER_MERGE_REQUIRED_MESSAGE = (
    "当前执行器只能部署已合并的模型。请先在「导出/模型导出」页对 LoRA 制品执行"
    "「合并 LoRA」，待合并产物为 ready 后，用它新建部署（不要勾选适配器）。"
)


class AdapterDeploymentStrategy(StrEnum):
    """How a deployment request may reference LoRA adapters."""

    MOUNT = "mount"
    MERGE_ONLY = "merge-only"


def normalize_adapter_strategy(value: str | None) -> AdapterDeploymentStrategy:
    """Parse a configuration value, defaulting to ``mount`` (engine capability).

    Raises:
        ValueError: when the value is neither empty nor a known strategy.
    """
    if value is None or not str(value).strip():
        return AdapterDeploymentStrategy.MOUNT
    normalized = str(value).strip().lower()
    try:
        return AdapterDeploymentStrategy(normalized)
    except ValueError as exc:
        allowed = " or ".join(strategy.value for strategy in AdapterDeploymentStrategy)
        raise ValueError(f"adapter deployment strategy must be {allowed}") from exc


def requested_adapter_ids(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Adapter artifact ids a task-create payload asks to mount.

    Tolerates the shapes a form can send: ``config.adapter_ids`` as a single
    string or a list, with blank entries dropped.
    """
    if not isinstance(payload, Mapping):
        return ()
    config = payload.get("config")
    if not isinstance(config, Mapping):
        return ()
    raw = config.get("adapter_ids")
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, (list, tuple)):
        candidates = raw
    else:
        return ()
    return tuple(str(item).strip() for item in candidates if str(item).strip())


def adapter_deployment_violation(
    strategy: AdapterDeploymentStrategy | str,
    *,
    task_type: str | None,
    payload: Mapping[str, Any] | None,
) -> str | None:
    """Return the operator-facing refusal message, or ``None`` when allowed.

    Only inference deployments consume adapters; training and evaluation
    requests legitimately carry adapter ids and must not be blocked.
    """
    resolved = AdapterDeploymentStrategy(strategy)
    if resolved is not AdapterDeploymentStrategy.MERGE_ONLY:
        return None
    if str(task_type or "").strip().lower() != "inference":
        return None
    if not requested_adapter_ids(payload):
        return None
    return ADAPTER_MERGE_REQUIRED_MESSAGE
