"""Adapter deployment rules — the shared half of the DCU merge-only gate."""

import pytest

from model_access import (
    ADAPTER_MERGE_REQUIRED_MESSAGE,
    AdapterDeploymentStrategy,
    adapter_deployment_violation,
    normalize_adapter_strategy,
    requested_adapter_ids,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, AdapterDeploymentStrategy.MOUNT),
        ("", AdapterDeploymentStrategy.MOUNT),
        ("   ", AdapterDeploymentStrategy.MOUNT),
        ("mount", AdapterDeploymentStrategy.MOUNT),
        ("MERGE-ONLY", AdapterDeploymentStrategy.MERGE_ONLY),
        (" merge-only ", AdapterDeploymentStrategy.MERGE_ONLY),
    ],
)
def test_normalize_adapter_strategy(value, expected):
    assert normalize_adapter_strategy(value) is expected


def test_normalize_adapter_strategy_rejects_unknown():
    with pytest.raises(ValueError, match="mount or merge-only"):
        normalize_adapter_strategy("hot-swap")


@pytest.mark.parametrize(
    "payload,expected",
    [
        (None, ()),
        ({}, ()),
        ({"config": None}, ()),
        ({"config": "adapter-1"}, ()),
        ({"config": {"adapter_ids": "adapter-1"}}, ("adapter-1",)),
        ({"config": {"adapter_ids": ["a", " b ", ""]}}, ("a", "b")),
        ({"config": {"adapters": ["a"]}}, ()),
    ],
)
def test_requested_adapter_ids(payload, expected):
    assert requested_adapter_ids(payload) == expected


def test_merge_only_refuses_adapter_on_inference():
    message = adapter_deployment_violation(
        AdapterDeploymentStrategy.MERGE_ONLY,
        task_type="inference",
        payload={"config": {"adapter_ids": ["adapter-1"]}},
    )
    assert message == ADAPTER_MERGE_REQUIRED_MESSAGE


@pytest.mark.parametrize("task_type", ["training", "eval", None, ""])
def test_merge_only_ignores_non_inference(task_type):
    assert (
        adapter_deployment_violation(
            AdapterDeploymentStrategy.MERGE_ONLY,
            task_type=task_type,
            payload={"config": {"adapter_ids": ["adapter-1"]}},
        )
        is None
    )


def test_merge_only_allows_plain_deployment():
    assert (
        adapter_deployment_violation(
            AdapterDeploymentStrategy.MERGE_ONLY,
            task_type="inference",
            payload={"config": {"npu_cards": 1}},
        )
        is None
    )


def test_mount_strategy_never_refuses():
    assert (
        adapter_deployment_violation(
            "mount",
            task_type="inference",
            payload={"config": {"adapter_ids": ["adapter-1"]}},
        )
        is None
    )
