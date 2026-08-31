BEGIN;

CREATE TABLE IF NOT EXISTS provider_credential (
    credential_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    owner_user_id VARCHAR(128),
    plugin_id VARCHAR(160) NOT NULL,
    provider_id VARCHAR(80) NOT NULL,
    name VARCHAR(64) NOT NULL,
    base_url VARCHAR(2048) NOT NULL,
    encrypted_values TEXT NOT NULL,
    api_key_masked VARCHAR(128) NOT NULL,
    key_version INTEGER NOT NULL DEFAULT 1,
    scope VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL,
    deployment JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_credential_tenant_owner
    ON provider_credential (tenant_id, owner_user_id);
CREATE INDEX IF NOT EXISTS ix_credential_provider
    ON provider_credential (tenant_id, provider_id);

CREATE TABLE IF NOT EXISTS configured_model (
    configured_model_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    owner_user_id VARCHAR(128),
    credential_id VARCHAR(64) NOT NULL REFERENCES provider_credential(credential_id),
    plugin_id VARCHAR(160) NOT NULL,
    provider_id VARCHAR(80) NOT NULL,
    provider_display_name VARCHAR(160) NOT NULL,
    model VARCHAR(256) NOT NULL,
    label VARCHAR(256),
    model_type VARCHAR(40) NOT NULL,
    categories JSONB NOT NULL DEFAULT '[]'::jsonb,
    input_modalities JSONB NOT NULL DEFAULT '[]'::jsonb,
    output_modalities JSONB NOT NULL DEFAULT '[]'::jsonb,
    features JSONB NOT NULL DEFAULT '[]'::jsonb,
    operations JSONB NOT NULL DEFAULT '[]'::jsonb,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    parameter_schema JSONB,
    context_window INTEGER,
    max_output_tokens INTEGER,
    protocol_versions JSONB NOT NULL DEFAULT '["1.1"]'::jsonb,
    status VARCHAR(32) NOT NULL,
    source VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_credential_model_type UNIQUE (credential_id, model, model_type)
);
CREATE INDEX IF NOT EXISTS ix_model_tenant_owner
    ON configured_model (tenant_id, owner_user_id);
CREATE INDEX IF NOT EXISTS ix_model_catalog_filter
    ON configured_model (tenant_id, provider_id, model_type, status);

CREATE TABLE IF NOT EXISTS model_invocation_usage (
    invocation_id VARCHAR(160) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    session_id VARCHAR(160),
    query_id VARCHAR(160),
    app_id VARCHAR(128),
    configured_model_id VARCHAR(64) NOT NULL,
    operation VARCHAR(40) NOT NULL,
    usage JSONB,
    cost DOUBLE PRECISION,
    latency_ms INTEGER,
    status VARCHAR(32) NOT NULL,
    trace_id VARCHAR(64),
    error_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_usage_tenant_query
    ON model_invocation_usage (tenant_id, session_id, query_id);

CREATE TABLE IF NOT EXISTS model_registration_audit (
    audit_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    operator_user_id VARCHAR(128),
    credential_id VARCHAR(64),
    action VARCHAR(64) NOT NULL,
    result VARCHAR(32) NOT NULL,
    request_id VARCHAR(160),
    trace_id VARCHAR(64),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS model_access_idempotency (
    record_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    idempotency_key VARCHAR(256) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_idempotency_scope UNIQUE (tenant_id, user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS model_invocation_binding (
    invocation_id VARCHAR(160) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    session_id VARCHAR(160),
    query_id VARCHAR(160),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMIT;

