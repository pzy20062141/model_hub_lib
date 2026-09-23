BEGIN;

-- Hosts aggregate invocation usage by a stable tenant/session/query context.
-- Keep the migration aligned with InvocationBindingRecord.__table_args__ so
-- existing databases and newly created schemas have the same lookup path.
CREATE INDEX IF NOT EXISTS ix_model_invocation_binding_context
    ON model_invocation_binding (tenant_id, session_id, query_id);

COMMIT;
