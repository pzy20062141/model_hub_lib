from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import Field
from sqlalchemy import URL, Engine, create_engine

from ..contracts.common import StrictModel
from ..protocols import SecretResolver


class DatabaseEndpoint(StrictModel):
    host: str
    port: int = Field(ge=1, le=65535)
    role: Literal["primary", "replica"] = "primary"
    zone: str | None = None
    weight: int = Field(default=100, ge=1)


class DatabasePoolConfig(StrictModel):
    min_size: int = Field(default=2, ge=0)
    max_size: int = Field(default=20, ge=1)
    connect_timeout_ms: int = Field(default=3000, ge=100)
    statement_timeout_ms: int = Field(default=30000, ge=100)
    idle_timeout_seconds: int = Field(default=300, ge=1)
    max_lifetime_seconds: int = Field(default=1800, ge=1)

    def model_post_init(self, __context: Any) -> None:
        if self.min_size > self.max_size:
            raise ValueError("pool.min_size cannot exceed pool.max_size")


class DatabaseConnectionConfig(StrictModel):
    name: str
    engine: Literal["postgresql", "mysql", "redis", "sqlite"]
    deployment: Literal["local", "cloud_cluster"] = "local"
    endpoints: list[DatabaseEndpoint] = []
    database: str | None = None
    schema_name: str | None = None
    username_ref: str | None = None
    password_ref: str | None = None
    tls_mode: Literal["disable", "prefer", "require", "verify_ca", "verify_full"] = "require"
    ca_cert_ref: str | None = None
    pool: DatabasePoolConfig = DatabasePoolConfig()
    required: bool = True
    extensions: dict[str, Any] = {}


class DatabaseSettings(StrictModel):
    databases: dict[str, DatabaseConnectionConfig]

    @classmethod
    def from_yaml(cls, path: str) -> DatabaseSettings:
        with open(path, encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        if not isinstance(payload, dict):
            raise ValueError("database configuration must be an object")
        databases = payload.get("databases", {})
        normalized = {name: {"name": name, **value} for name, value in databases.items()}
        return cls(databases=normalized)


class DatabaseRegistry:
    def __init__(self, settings: DatabaseSettings, secret_resolver: SecretResolver):
        self._settings = settings
        self._secret_resolver = secret_resolver
        self._engines: dict[str, Engine] = {}

    def connect_sql(self, logical_name: str) -> Engine:
        if logical_name in self._engines:
            return self._engines[logical_name]
        config = self._settings.databases[logical_name]
        if config.engine == "redis":
            raise ValueError("redis uses a separate optional client")
        if config.engine == "sqlite":
            database = config.database or ":memory:"
            url = (
                "sqlite+pysqlite:///:memory:"
                if database == ":memory:"
                else f"sqlite+pysqlite:///{database}"
            )
            engine = create_engine(url, pool_pre_ping=True)
        else:
            primary = next((item for item in config.endpoints if item.role == "primary"), None)
            if not primary:
                raise ValueError(f"database {logical_name} has no primary endpoint")
            username = self._secret_resolver.resolve(config.username_ref or "")
            password = self._secret_resolver.resolve(config.password_ref or "")
            driver = "postgresql+psycopg" if config.engine == "postgresql" else "mysql+pymysql"
            query: dict[str, str] = {}
            if config.engine == "postgresql" and config.tls_mode != "disable":
                query["sslmode"] = config.tls_mode
                if config.ca_cert_ref:
                    query["sslrootcert"] = self._secret_resolver.resolve(config.ca_cert_ref)
            url = URL.create(
                driver,
                username=username,
                password=password,
                host=primary.host,
                port=primary.port,
                database=config.database,
                query=query,
            )
            connect_args: dict[str, Any] = {
                "connect_timeout": max(1, config.pool.connect_timeout_ms // 1000)
            }
            if config.engine == "mysql" and config.tls_mode != "disable":
                ssl_options: dict[str, Any] = {}
                if config.ca_cert_ref:
                    ssl_options["ca"] = self._secret_resolver.resolve(config.ca_cert_ref)
                connect_args["ssl"] = ssl_options
            engine = create_engine(
                url,
                pool_size=config.pool.min_size,
                max_overflow=max(0, config.pool.max_size - config.pool.min_size),
                pool_recycle=config.pool.max_lifetime_seconds,
                pool_pre_ping=True,
                connect_args=connect_args,
            )
        self._engines[logical_name] = engine
        return engine

    def close(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
