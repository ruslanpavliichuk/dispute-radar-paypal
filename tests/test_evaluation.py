from fastapi.testclient import TestClient

from api.main import app


def test_evaluation_reads_the_model_cards(monkeypatch):
    from api.config import settings
    monkeypatch.setattr(settings, "paypal_client_id", "id")
    monkeypatch.setattr(settings, "paypal_client_secret", "secret")
    with TestClient(app) as client:
        body = client.get("/api/evaluation").json()
    assert body["risk"]["calibration"] and body["outcome"]["calibration"]
    assert "pr_auc" in body["risk"]["metrics"]
    assert body["benchmark"]["runs"]
    curve = body["decision"]["curve"]
    assert curve[0]["action"] == "offer" and curve[-1]["action"] == "fight"  # sure loss: offer; sure win: fight


def test_database_url_gets_tls_and_the_psycopg_driver_unless_local():
    from sqlalchemy.engine import make_url
    from api.db import make_engine, tls_args
    assert make_engine("postgres://u:p@db.example.com/x").url.drivername == "postgresql+psycopg"
    assert tls_args(make_url("postgresql://u:p@db.example.com/x")) == {"sslmode": "require"}
    assert tls_args(make_url("postgresql+psycopg://u:p@localhost/x")) == {}
    assert tls_args(make_url("postgresql://u:p@db.example.com/x?sslmode=verify-full")) == {}  # the URL's own choice wins
