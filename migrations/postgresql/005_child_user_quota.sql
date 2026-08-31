-- v0.3: tenant child-user budget and cost accounting.
-- Provider quota tables from older installations may be dropped separately after
-- confirming no older application instance still uses them.

CREATE TABLE IF NOT EXISTS model_credit_rate (
    tenant_id VARCHAR(128) NOT NULL,
    configured_model_id VARCHAR(64) NOT NULL REFERENCES configured_model(configured_model_id) ON DELETE CASCADE,
    per_request_credits NUMERIC(20, 6) NOT NULL DEFAULT 1,
    input_credits_per_1k NUMERIC(20, 6) NOT NULL DEFAULT 0,
    output_credits_per_1k NUMERIC(20, 6) NOT NULL DEFAULT 0,
    billable_unit_credits NUMERIC(20, 6) NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, configured_model_id)
);

CREATE TABLE IF NOT EXISTS user_quota_template (
    template_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    name VARCHAR(128) NOT NULL,
    period_type VARCHAR(16) NOT NULL CHECK (period_type IN ('DAY', 'MONTH')),
    credit_limit NUMERIC(20, 6),
    soft_limit_percent INTEGER NOT NULL DEFAULT 80 CHECK (soft_limit_percent BETWEEN 1 AND 99),
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_quota_template_default
    ON user_quota_template(tenant_id, is_default, enabled);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_quota_one_default
    ON user_quota_template(tenant_id) WHERE is_default AND enabled;

CREATE TABLE IF NOT EXISTS user_quota_role_binding (
    tenant_id VARCHAR(128) NOT NULL,
    role_code VARCHAR(128) NOT NULL,
    template_id VARCHAR(64) NOT NULL REFERENCES user_quota_template(template_id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, role_code)
);

CREATE TABLE IF NOT EXISTS user_quota_assignment (
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    template_id VARCHAR(64) REFERENCES user_quota_template(template_id) ON DELETE SET NULL,
    override_mode VARCHAR(16) NOT NULL CHECK (override_mode IN ('INHERIT', 'LIMITED', 'UNLIMITED')),
    credit_limit NUMERIC(20, 6),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_quota_period (
    period_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    period_type VARCHAR(16) NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    credit_limit NUMERIC(20, 6),
    credits_used NUMERIC(20, 6) NOT NULL DEFAULT 0,
    credits_reserved NUMERIC(20, 6) NOT NULL DEFAULT 0,
    source_type VARCHAR(24) NOT NULL,
    source_id VARCHAR(128),
    soft_limit_percent INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, user_id, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS ix_user_quota_period_lookup
    ON user_quota_period(tenant_id, user_id, period_end);

CREATE TABLE IF NOT EXISTS user_quota_reservation (
    invocation_id VARCHAR(160) PRIMARY KEY,
    period_id VARCHAR(64) NOT NULL REFERENCES user_quota_period(period_id),
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    configured_model_id VARCHAR(64) NOT NULL,
    operation VARCHAR(40) NOT NULL,
    estimated_credits NUMERIC(20, 6) NOT NULL,
    actual_credits NUMERIC(20, 6),
    estimated_usage JSONB,
    rate_snapshot JSONB NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_user_quota_reservation_user
    ON user_quota_reservation(tenant_id, user_id);

CREATE TABLE IF NOT EXISTS user_cost_ledger (
    invocation_id VARCHAR(160) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    configured_model_id VARCHAR(64) NOT NULL,
    operation VARCHAR(40) NOT NULL,
    usage JSONB,
    credits NUMERIC(20, 6) NOT NULL,
    rate_snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_user_cost_query
    ON user_cost_ledger(tenant_id, user_id, created_at);

CREATE TABLE IF NOT EXISTS user_quota_audit (
    audit_id VARCHAR(64) PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL,
    operator_user_id VARCHAR(128),
    action VARCHAR(64) NOT NULL,
    target_id VARCHAR(256) NOT NULL,
    before JSONB,
    after JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
