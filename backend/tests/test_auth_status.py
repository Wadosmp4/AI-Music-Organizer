from fastapi.testclient import TestClient

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.main import app


def test_auth_status_reports_all_states_independently():
    auth_status_store.set_write_status(AuthStatus.NEEDS_RECONNECT, "cookie expired")
    auth_status_store.set_detection_status(AuthStatus.OK)
    dependency_health_store.set_status("llm", DependencyStatus.DEGRADED, "rate limited")

    client = TestClient(app)
    response = client.get("/api/v1/auth-status")

    assert response.status_code == 200
    body = response.json()
    assert body["write_path"] == {"status": "needs_reconnect", "reason": "cookie expired"}
    assert body["detection_path"] == {"status": "ok", "reason": None}
    assert body["llm"] == {"status": "degraded", "reason": "rate limited"}

    auth_status_store.set_write_status(AuthStatus.OK)
    dependency_health_store.set_status("llm", DependencyStatus.OK)
