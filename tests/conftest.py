import pytest

from api.config import settings


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    # TestClient(app) runs the lifespan, which opens the database; never touch Postgres in tests
    monkeypatch.setattr(settings, "database_url", "sqlite://")
