from fastapi.testclient import TestClient

from app.integrations.auth_status import AuthStatus, auth_status_store
from app.integrations.dependency_health import DependencyStatus, dependency_health_store
from app.main import app


def test_auth_status_reports_all_states_independently():
    auth_status_store.set_write_status(AuthStatus.NEEDS_RECONNECT, "cookie expired")
    auth_status_store.set_detection_status(AuthStatus.OK)
    dependency_health_store.set_status("llm_description_match", DependencyStatus.DEGRADED, "rate limited")

    client = TestClient(app)
    response = client.get("/api/v1/auth-status")

    assert response.status_code == 200
    body = response.json()
    assert body["write_path"] == {"status": "needs_reconnect", "reason": "cookie expired"}
    assert body["detection_path"] == {"status": "ok", "reason": None}
    assert body["llm_description_match"] == {"status": "degraded", "reason": "rate limited"}

    auth_status_store.set_write_status(AuthStatus.OK)
    dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)


def test_auth_status_never_blends_the_three_independent_llm_use_cases():
    """KTD17/18: a persistent failure in one LLM use case (e.g. clustering)
    must not be masked by a different use case (e.g. BPM estimate) succeeding
    — each has its own health-store key and its own field in the response."""
    dependency_health_store.set_status("llm_bpm_estimate", DependencyStatus.OK)
    dependency_health_store.set_status(
        "llm_description_match", DependencyStatus.DEGRADED, "description match failed"
    )
    dependency_health_store.set_status("llm_clustering", DependencyStatus.OK)

    client = TestClient(app)
    body = client.get("/api/v1/auth-status").json()

    assert body["llm_bpm_estimate"]["status"] == "ok"
    assert body["llm_description_match"]["status"] == "degraded"
    assert body["llm_clustering"]["status"] == "ok"

    dependency_health_store.set_status("llm_description_match", DependencyStatus.OK)
