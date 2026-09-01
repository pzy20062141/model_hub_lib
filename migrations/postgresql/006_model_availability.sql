BEGIN;

CREATE TABLE IF NOT EXISTS tenant_provider_status (
    tenant_id VARCHAR(128) NOT NULL,
    plugin_id VARCHAR(160) NOT NULL,
    provider_id VARCHAR(80) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by_user_id VARCHAR(128),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, plugin_id, provider_id)
);

COMMIT;
