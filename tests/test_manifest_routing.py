from __future__ import annotations

from model_access.adapters import load_provider_manifest
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
