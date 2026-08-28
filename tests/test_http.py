from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(make_settings):
    app = create_app(make_settings(), start_worker=False)
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_200_even_with_no_database(client) -> None:
    """Render must not kill the deploy because Supabase is unreachable."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "not_configured"
    assert body["mode"] == "shadow"


def test_ready_reports_503_and_says_why(client) -> None:
    response = client.get("/ready")
    assert response.status_code == 503
    assert "DATABASE_URL not configured" in str(response.json()["detail"])


def test_root_is_not_an_error_page(client) -> None:
    body = client.get("/").json()
    assert body["status"] == "alive"
    assert "/health" in body["endpoints"].values()


def test_status_exposes_the_ladder_and_masks_secrets(client) -> None:
    body = client.get("/status").json()
    assert body["stage_ladder"][0] == "discovered"
    assert body["stage_ladder"][-1] == "completed"
    assert body["config"]["database_url"] == "MISSING"
    assert body["outbound_telegram_actions"] is False


def test_status_reports_the_control_bot_as_not_configured(client) -> None:
    """The bot is optional; its absence has to be visible as a name to set."""
    body = client.get("/status").json()["control_bot"]
    assert body["running"] is False
    assert "TELEGRAM_BOT_TOKEN" in body["how"] and "BotFather" in body["how"]


def test_a_token_without_owner_ids_is_reported_as_refusing_to_start(make_settings) -> None:
    """Fail-closed is only honest if it is *visible*: a silent refusal to answer is
    indistinguishable from a broken deploy."""
    with TestClient(
        create_app(
            make_settings(telegram_bot_token="123456:" + "a" * 25, telegram_owner_user_ids=None),
            start_worker=False,
        )
    ) as client:
        body = client.get("/status").json()
    assert body["control_bot"]["state"] == "refused to start"
    assert "TELEGRAM_OWNER_USER_IDS" in body["control_bot"]["why"]
    assert body["config"]["telegram_bot_token"] == "configured"


def test_stage_endpoint_matches_ladder(client) -> None:
    from app.stages import LADDER

    assert client.get("/api/stages").json()["stages"] == [s.value for s in LADDER]


def test_openapi_documents_the_control_plane(client) -> None:
    spec = client.get("/openapi.json").json()
    assert "/control/pause" in spec["paths"]
    assert "/health" in spec["paths"]


class TestControlAuth:
    def test_pause_requires_a_configured_token(self, make_settings) -> None:
        with TestClient(create_app(make_settings(), start_worker=False)) as client:
            response = client.post("/control/pause", json={"reason": "stop"})
            # Fail closed: with no CONTROL_TOKEN the endpoint is unavailable
            # rather than open.
            assert response.status_code == 503

    def test_missing_header_is_401(self, make_settings) -> None:
        with TestClient(create_app(make_settings(control_token="t" * 40), start_worker=False)) as client:
            assert client.post("/control/pause").status_code == 401

    def test_wrong_token_is_403(self, make_settings) -> None:
        with TestClient(create_app(make_settings(control_token="t" * 40), start_worker=False)) as client:
            response = client.post("/control/pause", headers={"Authorization": "Bearer nope"})
            assert response.status_code == 403

    def test_correct_token_reaches_the_database(self, make_settings, monkeypatch) -> None:
        calls = {}

        class FakeDB:
            state = "up"
            connected = True

            async def set_paused(self, paused, reason=None):
                calls["paused"] = (paused, reason)
                return {"paused": paused}

            async def connect(self):
                return False

            async def close(self):
                return None

        settings = make_settings(control_token="right" + "0" * 40)
        app = create_app(settings, start_worker=False)
        app.state.db = FakeDB()
        with TestClient(app) as client:
            response = client.post(
                "/control/pause",
                json={"reason": "suspicious forwarding"},
                headers={"Authorization": "Bearer right" + "0" * 40},
            )
            assert response.status_code == 200
        assert calls["paused"] == (True, "suspicious forwarding")

    @pytest.mark.parametrize(
        "path",
        ["/control/pause", "/control/resume", "/control/reconcile", "/control/shutdown"],
    )
    def test_every_control_endpoint_demands_a_token(self, make_settings, path) -> None:
        """Auth is uniform: a future endpoint that forgets the guard fails here."""
        with TestClient(create_app(make_settings(control_token="t" * 40), start_worker=False)) as client:
            response = client.post(path)
            assert response.status_code == 401, f"{path} is missing the control guard"
