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
