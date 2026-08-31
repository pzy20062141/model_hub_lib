from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field

from ..contracts.common import StrictModel
from ..contracts.entities import (
    I18nObject,
    ModelDescriptor,
    ProviderCapabilities,
    ProviderDescriptor,
    ProviderRef,
)
from ..contracts.enums import ModelCategory, ModelOperation, ModelStatus, ModelType


class ManifestModel(StrictModel):
    model: str
    model_type: ModelType
    label: str | None = None
    status: ModelStatus = ModelStatus.ACTIVE
    features: set[str] = set()
    input_modalities: set[Literal["text", "image", "audio", "video"]] = {"text"}
    output_modalities: set[Literal["text", "json", "vector", "image", "audio", "video"]] = {"text"}
    categories: set[ModelCategory] = set()
    operations: set[ModelOperation] = set()
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    properties: dict[str, Any] = {}
    parameter_schema: dict[str, Any] | None = None


class ProviderManifest(StrictModel):
    protocol_version: str = "1.1"
    provider: ProviderRef
    display_name: I18nObject
    supported_model_types: list[ModelType]
    capabilities: ProviderCapabilities = ProviderCapabilities()
    dynamic_model_discovery: bool = True
    models: list[ManifestModel]

    def build(self) -> tuple[ProviderDescriptor, list[ModelDescriptor]]:
        models = [
            ModelDescriptor(
                provider=self.provider,
                model=item.model,
                model_type=item.model_type,
                label=item.label,
                status=item.status,
                features=item.features,
                input_modalities=item.input_modalities,
                output_modalities=item.output_modalities,
                categories=item.categories,
                operations=item.operations,
                context_window=item.context_window,
                max_output_tokens=item.max_output_tokens,
                properties=item.properties,
                parameter_schema=item.parameter_schema,
                protocol_versions={self.protocol_version},
            )
            for item in self.models
        ]
        descriptor = ProviderDescriptor(
            provider=self.provider,
            display_name=self.display_name,
            supported_model_types=self.supported_model_types,
            capabilities=self.capabilities,
            dynamic_model_discovery=self.dynamic_model_discovery,
            protocol_version=self.protocol_version,
            models=models,
        )
        return descriptor, models


def load_provider_manifest(path: str | Path) -> ProviderManifest:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return ProviderManifest.model_validate(payload)
