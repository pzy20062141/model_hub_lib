from __future__ import annotations

import json
from pathlib import Path

from model_access import FernetCredentialCipher, ModelRepositoryClient, URLSecurityPolicy
from model_access.api import create_app


def main() -> None:
    client = ModelRepositoryClient.sqlite(
        encryption_key=FernetCredentialCipher.generate_key(),
        url_policy=URLSecurityPolicy(resolve_dns=False),
    )
    app = create_app(client)
    destination = Path("openapi/model_access-v1.1.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
