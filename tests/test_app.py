import json
import wave
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "test.db", tmp_path / "vault", "test-secret"))


def test_live_segments_keep_order_and_create_explicit_route(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/webhooks/omi/test-secret/live?uid=me&session_id=call-1",
        json=[{"text": "Первая мысль", "start": 1}, {"text": "в буфер: важная фраза", "start": 2}],
    )
    assert response.status_code == 200
    assert response.json()["routes"] == [{"kind": "clipboard", "target": "", "content": "важная фраза"}]

    session = client.get("/api/sessions/call-1").json()
    assert [item["text"] for item in session["segments"]] == ["Первая мысль", "в буфер: важная фраза"]
    assert session["routes"][0]["kind"] == "clipboard"


def test_pcm16_chunks_make_valid_wav_and_mirror_context(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.put("/api/settings/drive-mirror", json={"path": str(tmp_path)})
    url = "/api/webhooks/omi/test-secret/audio?uid=me&session_id=audio-1&sample_rate=16000"
    audio = (b"\x00\x00\x10\x00" * 800)
    result = client.post(url, content=audio)
    assert result.status_code == 200

    client.post("/api/webhooks/omi/test-secret/live?uid=me&session_id=audio-1", json=[{"text": "в заметки: проверить запись", "speaker_name": "Иван"}])
    exported = client.post("/api/sessions/audio-1/export")
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["mirror"]["status"] == "mirrored"
    assert {"recording.wav", "transcript.md", "context.json", "manifest.json"}.issubset(payload["files"])

    session = client.get("/api/sessions/audio-1").json()
    with wave.open(session["wav_path"], "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 1600
    context = json.loads(Path(payload["local_path"], "context.json").read_text(encoding="utf-8"))
    assert context["participants"][0]["name"] == "Иван"


def test_bad_secret_and_bad_pcm_are_rejected(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    assert client.post("/api/webhooks/omi/nope/audio", content=b"\x00\x00").status_code == 401
    assert client.post("/api/webhooks/omi/test-secret/audio", content=b"x").status_code == 422


def test_ask_prepares_grounded_prompt_without_sending_data(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post(
        "/api/webhooks/omi/test-secret/live?uid=me&session_id=context-1",
        json=[{"text": "Марина назвала бюджет в сто тысяч рублей.", "speaker_name": "Марина"}],
    )
    response = client.post("/api/ask", json={"question": "Какой бюджет назвала Марина?", "session_ids": ["context-1"]})
    assert response.status_code == 200
    assert response.json()["sent"] is False
    assert "[context-1#0]" in response.json()["prompt"]


def test_note_route_exports_only_after_explicit_request(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.put("/api/settings/notes-root", json={"path": str(tmp_path)})
    client.post("/api/webhooks/omi/test-secret/live?session_id=note-1", json=[{"text": "в заметки: купить кабель"}])
    route = client.get("/api/routes").json()[0]
    result = client.post(f"/api/routes/{route['id']}/export-note")
    assert result.status_code == 200
    assert Path(result.json()["path"]).read_text(encoding="utf-8").startswith("# Голосовая заметка")


def test_demo_data_is_local_and_creates_a_session(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post("/api/demo/seed")
    assert response.status_code == 200
    assert response.json()["local_only"] is True
    assert client.get("/api/sessions/demo-local-capture").status_code == 200


def test_completed_omi_memory_preserves_its_original_structured_context(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    memory = {
        "id": "memory-42",
        "transcript_segments": [{"text": "Подтверждаем бюджет", "speaker_name": "Марина", "start": 0}],
        "structured": {"title": "Бюджет", "overview": "Итог разговора", "action_items": [{"description": "Отправить смету"}]},
    }
    response = client.post("/api/webhooks/omi/test-secret/memory?uid=me", json=memory)
    assert response.status_code == 200
    session = client.get("/api/sessions/omi-memory-42").json()
    assert session["context"]["omi_structured_source"]["title"] == "Бюджет"
    assert session["context"]["omi_structured_source"]["action_items"][0]["description"] == "Отправить смету"


def test_omi_chat_tools_only_search_the_paired_users_archive(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/webhooks/omi/test-secret/memory?uid=owner",
        json={
            "id": "budget-42",
            "transcript_segments": [{"text": "Марина подтвердила бюджет сто тысяч", "speaker_name": "Марина"}],
            "structured": {"title": "Бюджет", "overview": "Подтверждено 100 000 рублей"},
        },
    )
    assert response.status_code == 200

    # A second user's session may exist in storage, but never appears in the owner's chat-tool result.
    other = client.app.state.store.active_session("other", "other-budget")
    client.app.state.store.append_segments(other, [{"text": "Чужой бюджет девять миллионов"}])

    manifest = client.get("/.well-known/omi-tools.json")
    assert manifest.status_code == 200
    assert {tool["name"] for tool in manifest.json()["tools"]} == {
        "search_field_archive",
        "build_field_dossier",
        "make_obsidian_draft",
        "field_relay_status",
    }

    result = client.post(
        "/tools/search-field-archive",
        json={"uid": "owner", "args": {"query": "бюджет"}},
    )
    assert result.status_code == 200
    assert "omi-budget-42" in result.json()["result"]
    assert "девять миллионов" not in result.json()["result"]

    blocked = client.post(
        "/tools/search-field-archive",
        json={"uid": "other", "args": {"query": "бюджет"}},
    )
    assert blocked.status_code == 403


def test_remote_dashboard_api_is_hidden_without_its_separate_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OMI_BRIDGE_PUBLIC_URL", "https://relay.example")
    monkeypatch.setenv("OMI_BRIDGE_DASHBOARD_KEY", "dashboard-secret")
    client = TestClient(create_app(tmp_path / "test.db", tmp_path / "vault", "test-secret"))

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code == 404
    assert client.get("/api/sessions", headers={"X-Omi-Bridge-Key": "dashboard-secret"}).status_code == 200


def test_live_answer_radar_builds_one_short_omi_notification_and_learns_style(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/webhooks/omi/test-secret/live?uid=me&session_id=father-test",
        json=[{"text": "Ты сможешь приехать сегодня?", "speaker_name": "Папа", "is_user": False, "start": 1}],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["notification"]["params"] == ["user_name", "user_facts", "user_context"]
    assert "140" in payload["notification"]["prompt"]
    assert payload["field"]["speaker"] == "Папа"

    profile = client.get("/api/field/people?uid=me")
    assert profile.status_code == 200
    assert profile.json()[0]["name"] == "Папа"
    assert profile.json()[0]["style"]["confidence"] == "низкая"

    # The same Omi delivery is idempotent and must never make another push request.
    duplicate = client.post(
        "/api/webhooks/omi/test-secret/live?uid=me&session_id=father-test",
        json=[{"text": "Ты сможешь приехать сегодня?", "speaker_name": "Папа", "is_user": False, "start": 1}],
    )
    assert duplicate.status_code == 200
    assert "notification" not in duplicate.json()


def test_field_mode_stops_live_answer_but_keeps_the_source_conversation(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/api/webhooks/omi/test-secret/live?uid=me&session_id=pair", json=[{"text": "Привет", "is_user": True}])
    mode = client.put("/api/field/mode", json={"uid": "me", "mode": "stop"})
    assert mode.status_code == 200
    response = client.post(
        "/api/webhooks/omi/test-secret/live?uid=me&session_id=quiet-test",
        json=[{"text": "Ты будешь сегодня?", "speaker_name": "Папа", "is_user": False}],
    )
    assert response.status_code == 200
    assert "notification" not in response.json()
    assert client.get("/api/sessions/quiet-test").status_code == 200
