from __future__ import annotations

import os

from model_access import ModelRepositoryClient, URLSecurityPolicy
from model_access.adapters import OpenAICompatibleAdapter, load_provider_manifest
from model_access.api import create_app

manifest = load_provider_manifest("config/providers/openai-compatible.yaml")
provider_descriptor, models = manifest.build()

client = ModelRepositoryClient.sqlite(
    os.getenv("MODEL_ACCESS_SQLITE_PATH", "model_access.db"),
    encryption_key=os.environ["MODEL_ACCESS_MASTER_KEY"],
    url_policy=URLSecurityPolicy(),
)
client.register_adapter(
    OpenAICompatibleAdapter(
        descriptor=provider_descriptor,
        model_manifest=models,
    )
)

app = create_app(client)
