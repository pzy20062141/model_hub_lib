BEGIN;

CREATE TABLE IF NOT EXISTS provider_preference (
    tenant_id VARCHAR(128) NOT NULL,
    plugin_id VARCHAR(160) NOT NULL,
    provider_id VARCHAR(80) NOT NULL,
    preferred_provider_type VARCHAR(16) NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, plugin_id, provider_id)
);

CREATE TABLE IF NOT EXISTS provider_quota (
    quota_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    plugin_id VARCHAR(160) NOT NULL,
    provider_id VARCHAR(80) NOT NULL,
    quota_type VARCHAR(16) NOT NULL,
    quota_unit VARCHAR(16) NOT NULL,
    quota_limit BIGINT NOT NULL,
    quota_used BIGINT NOT NULL DEFAULT 0,
    quota_reserved BIGINT NOT NULL DEFAULT 0,
    restrict_models JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_valid BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_provider_quota_limit CHECK (quota_limit >= -1),
    CONSTRAINT ck_provider_quota_used CHECK (quota_used >= 0),
    CONSTRAINT ck_provider_quota_reserved CHECK (quota_reserved >= 0)
);
CREATE INDEX IF NOT EXISTS ix_provider_quota_selection
    ON provider_quota (tenant_id, plugin_id, provider_id, is_valid, quota_type);

CREATE TABLE IF NOT EXISTS quota_reservation (
    invocation_id VARCHAR(160) PRIMARY KEY,
    quota_id VARCHAR(64) REFERENCES provider_quota(quota_id),
    tenant_id VARCHAR(128) NOT NULL,
    configured_model_id VARCHAR(64) NOT NULL,
    provider_type VARCHAR(16) NOT NULL,
    quota_type VARCHAR(16),
    quota_unit VARCHAR(16),
    reserved_units BIGINT NOT NULL DEFAULT 0,
    actual_units BIGINT,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_quota_reservation_quota_id ON quota_reservation (quota_id);
CREATE INDEX IF NOT EXISTS ix_quota_reservation_tenant_id ON quota_reservation (tenant_id);

CREATE TABLE IF NOT EXISTS configuration_source_version (
    source_key VARCHAR(512) PRIMARY KEY,
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS model_access_outbox (
    event_id VARCHAR(64) PRIMARY KEY,
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(512) NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_model_access_outbox_unpublished
    ON model_access_outbox (published_at, created_at);

COMMIT;
