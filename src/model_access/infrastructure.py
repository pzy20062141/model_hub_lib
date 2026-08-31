from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts.responses import ArtifactRef, Usage


class NoOpQuotaManager:
    async def reserve(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        configured_model_id: str,
        operation: str,
    ) -> None:
        del invocation_id, tenant_id, configured_model_id, operation

    async def settle(self, *, invocation_id: str, usage: Usage | None, succeeded: bool) -> None:
        del invocation_id, usage, succeeded


class InMemoryTaskBackend:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}

    async def register_external_task(
        self,
        *,
        invocation_id: str,
        provider_task_id: str,
        result_type: str,
        tenant_id: str,
        user_id: str | None,
        trace_context: dict[str, str],
        provider_payload: dict[str, Any],
    ) -> str:
        task_id = f"task_{uuid4().hex}"
        self.tasks[task_id] = {
            "task_id": task_id,
            "invocation_id": invocation_id,
            "provider_task_id": provider_task_id,
            "result_type": result_type,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "trace_context": trace_context,
            "provider_payload": provider_payload,
            "status": "ACCEPTED",
        }
        return task_id


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self.items: dict[str, bytes | str] = {}

    async def put_bytes(
        self,
        *,
        data: bytes,
        media_type: str,
        filename: str | None,
        tenant_id: str,
        user_id: str | None,
    ) -> ArtifactRef:
        del filename, tenant_id, user_id
        artifact_id = f"art_{uuid4().hex}"
        self.items[artifact_id] = data
        return ArtifactRef(
            artifact_id=artifact_id, media_type=media_type, uri=f"memory://{artifact_id}"
        )

    async def register_uri(
        self,
        *,
        uri: str,
        media_type: str,
        tenant_id: str,
        user_id: str | None,
    ) -> ArtifactRef:
        del tenant_id, user_id
        artifact_id = f"art_{uuid4().hex}"
        self.items[artifact_id] = uri
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            uri=f"memory://{artifact_id}",
        )


class LocalArtifactStore:
    """Small local implementation for development; use BOS/S3 in production."""

    def __init__(self, root: str | Path, public_uri_prefix: str = "file://"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_uri_prefix = public_uri_prefix.rstrip("/")

    async def put_bytes(
        self,
        *,
        data: bytes,
        media_type: str,
        filename: str | None,
        tenant_id: str,
        user_id: str | None,
    ) -> ArtifactRef:
        del user_id
        artifact_id = f"art_{uuid4().hex}"
        extension = Path(filename or "").suffix or mimetypes.guess_extension(media_type) or ".bin"
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        directory = self.root / tenant_hash
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{artifact_id}{extension}"
        path.write_bytes(data)
        expires_at = datetime.now(UTC) + timedelta(days=7)
        uri = f"{self.public_uri_prefix}/{tenant_hash}/{path.name}"
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            uri=uri,
            expires_at=expires_at,
        )

    async def register_uri(
        self,
        *,
        uri: str,
        media_type: str,
        tenant_id: str,
        user_id: str | None,
    ) -> ArtifactRef:
        del user_id
        artifact_id = f"art_{uuid4().hex}"
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        directory = self.root / tenant_hash
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{artifact_id}.uri.json"
        path.write_text(
            json.dumps({"source_uri": uri, "media_type": media_type}, separators=(",", ":")),
            encoding="utf-8",
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type=media_type,
            uri=f"{self.public_uri_prefix}/{tenant_hash}/{path.name}",
        )
