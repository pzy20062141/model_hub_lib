BEGIN;

CREATE TABLE IF NOT EXISTS user_default_model (
    tenant_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    model_type VARCHAR(40) NOT NULL,
    configured_model_id VARCHAR(64) NOT NULL
        REFERENCES configured_model(configured_model_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, user_id, model_type)
);
CREATE INDEX IF NOT EXISTS ix_user_default_model_configured
    ON user_default_model (configured_model_id);

COMMIT;
