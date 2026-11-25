from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import healthcheck


@pytest.mark.asyncio
async def test_database_healthcheck_normalizes_postgres_url(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:pass@db.example/testdb"
        "?sslmode=require&channel_binding=require",
    )

    connection = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def connect():
        yield connection

    engine = SimpleNamespace(
        connect=connect,
        dispose=AsyncMock(),
    )
    captured = {}

    def create_engine(url, **kwargs):
        captured["url"] = url
        return engine

    monkeypatch.setattr(healthcheck, "create_async_engine", create_engine)

    assert await healthcheck.check_database()
    assert captured["url"] == (
        "postgresql+asyncpg://user:pass@db.example/testdb?ssl=require"
    )
    engine.dispose.assert_awaited_once()
