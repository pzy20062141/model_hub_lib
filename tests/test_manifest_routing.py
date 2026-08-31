from __future__ import annotations

import pytest

from model_access.adapters import (
    BUILTIN_PROVIDER_MANIFESTS,
    load_builtin_provider_manifest,
    load_provider_manifest,
)
from model_access.contracts.entities import SelfHostedDeployment, SelfHostedEndpoint
from model_access.contracts.enums import DeploymentMode, DeploymentProtocol, ModelCategory
from model_access.persistence.database import DatabaseSettings
from model_access.routing import SelfHostedEndpointRouter


def test_provider_manifest_builds_explicit_capabilities() -> None:
    manifest = load_provider_manifest("config/providers/openai-compatible.yaml")
    descriptor, models = manifest.build()
    assert descriptor.provider.provider_id == "openai_compatible"
    assert len(models) == 3
    assert ModelCategory.VISION_MODEL in models[0].categories


@pytest.mark.parametrize(
    ("path", "provider_id", "base_url", "expected_models", "dynamic_discovery"),
    [
        (
            "config/providers/deepseek.yaml",
            "deepseek",
            "https://api.deepseek.com",
            {"deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"},
            True,
        ),
        (
            "config/providers/baidu-qianfan.yaml",
            "baidu_qianfan",
            "https://qianfan.baidubce.com/v2",
            {"ernie-4.5-turbo-32k", "ernie-4.5-8k-preview"},
            True,
        ),
        (
            "config/providers/volcengine-ark.yaml",
            "volcengine_ark",
            "https://ark.cn-beijing.volces.com/api/v3",
            {"doubao-seed-2-1-pro-260628", "doubao-seed-2-1-turbo-260628"},
            False,
        ),
    ],
)
def test_builtin_compatible_provider_manifests(
    path: str,
    provider_id: str,
    base_url: str,
    expected_models: set[str],
    dynamic_discovery: bool,
) -> None:
    descriptor, models = load_provider_manifest(path).build()
    assert descriptor.provider.provider_id == provider_id
    assert descriptor.default_base_url == base_url
    assert descriptor.dynamic_model_discovery is dynamic_discovery
    assert {item.model for item in models} == expected_models
    assert {item.model_type for item in models} == {"text_generation"}


def test_packaged_manifests_match_editable_config_copies() -> None:
    for provider_id, filename in BUILTIN_PROVIDER_MANIFESTS.items():
        packaged = load_builtin_provider_manifest(provider_id)
        editable = load_provider_manifest(f"config/providers/{filename}")
        assert packaged == editable


def test_self_hosted_weighted_route_and_cooldown() -> None:
    deployment = SelfHostedDeployment(
        deployment_mode=DeploymentMode.KUBERNETES_SERVICE,
        protocol=DeploymentProtocol.OPENAI_COMPATIBLE,
        model_name="private-model",
        endpoints=[
            SelfHostedEndpoint(
                endpoint_id="primary",
                base_url="https://primary.internal/v1",
                weight=2,
            ),
            SelfHostedEndpoint(
                endpoint_id="secondary",
                base_url="https://secondary.internal/v1",
                weight=1,
            ),
        ],
    )
    router = SelfHostedEndpointRouter(failure_threshold=1, cooldown_seconds=60)
    selected = [router.select(deployment).endpoint_id for _ in range(6)]
    assert selected.count("primary") == 4
    assert selected.count("secondary") == 2

    router.report_failure("primary")
    assert router.select(deployment).endpoint_id == "secondary"


def test_database_configuration_is_split_by_logical_name() -> None:
    settings = DatabaseSettings.from_yaml("config/databases.example.yaml")
    assert set(settings.databases) == {"catalog", "usage", "audit", "cache"}
    assert settings.databases["catalog"].deployment == "local"
    assert settings.databases["usage"].deployment == "cloud_cluster"
    assert settings.databases["cache"].engine == "redis"
