from pathlib import Path

from sqlalchemy import create_engine, inspect

from model_access.persistence.repository import ModelAccessRepository


def test_invocation_binding_context_index_is_created_for_new_schemas() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ModelAccessRepository(engine, create_schema=True)

    indexes = {
        item["name"]: item["column_names"]
        for item in inspect(engine).get_indexes("model_invocation_binding")
    }

    assert indexes["ix_model_invocation_binding_context"] == [
        "tenant_id",
        "session_id",
        "query_id",
    ]


def test_postgresql_migration_adds_the_same_context_index_idempotently() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations/postgresql/007_invocation_binding_context_index.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE INDEX IF NOT EXISTS ix_model_invocation_binding_context" in migration
    assert "ON model_invocation_binding (tenant_id, session_id, query_id)" in migration
