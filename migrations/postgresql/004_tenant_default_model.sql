BEGIN;

CREATE TABLE IF NOT EXISTS tenant_default_model (
    tenant_id VARCHAR(128) NOT NULL,
    model_type VARCHAR(40) NOT NULL,
    configured_model_id VARCHAR(64) NOT NULL
        REFERENCES configured_model(configured_model_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, model_type)
);
CREATE INDEX IF NOT EXISTS ix_tenant_default_model_configured
    ON tenant_default_model (configured_model_id);

-- 旧表无法证明配置者是否为租户管理员，因此不能把任意子用户的个人选择
-- 自动升级为租户默认。保留为 legacy 表供审计或人工迁移，新表初始为空。
DO $$
BEGIN
    IF to_regclass('user_default_model') IS NOT NULL
        AND to_regclass('legacy_user_default_model') IS NULL THEN
        ALTER TABLE user_default_model RENAME TO legacy_user_default_model;
    END IF;
END $$;

COMMIT;
