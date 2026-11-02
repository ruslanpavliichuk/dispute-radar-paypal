import pytest

from api.config import settings


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    # TestClient(app) runs the lifespan, which opens the database; never touch Postgres in tests
    monkeypatch.setattr(settings, "database_url", "sqlite://")


@pytest.fixture(autouse=True)
def no_agent_loop(monkeypatch):
    # the lifespan would start the response-agent loop; agent tests call run_once directly
    monkeypatch.setattr(settings, "agent_enabled", False)


@pytest.fixture(autouse=True)
def fresh_reconcile_clock(monkeypatch):
    # run_once throttles the PENDING backstop; every test starts as if it had not run yet
    from agents import runner
    monkeypatch.setattr(runner, "_last_reconcile", float("-inf"))
