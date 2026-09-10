from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse


DEFAULT_SECRET = "local-development-key-change-me"
SESSION_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
GENERIC_SPEAKER = re.compile(r"^(speaker[_ -]?\d+|unknown|unknown speaker|you|ты)$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
QUOTED_RE = re.compile(r"«([^»]{2,80})»|\"([^\"]{2,80})\"")
TAG_RE = re.compile(r"(?<!\w)#([\w-]{2,50})", re.UNICODE)
ROUTE_RE = re.compile(
    r"^\s*(?:оми|omi)?[,:\s-]*(?:(?P<clipboard>в\s+буфер|буфер)|(?P<notes>в\s+заметки|заметка)|"
    r"(?P<ai>запрос\s+(?P<target>[\w .-]{1,40})))\s*[:—–,-]\s*(?P<content>.+)$",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_text(value: Any, limit: int = 20_000) -> str:
    return str(value or "").strip()[:limit]


def safe_session_id(value: str) -> str:
    cleaned = SESSION_ID_RE.sub("-", value).strip("-")[:120]
    if not cleaned:
        return f"s-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    return cleaned


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def build_context_prompt(question: str, sessions: list[dict[str, Any]]) -> str:
    sources: list[str] = []
    for session in sessions:
        for index, segment in enumerate(session["segments"]):
            text = safe_text(segment.get("text"), 4_000)
            if text:
                speaker = f"{segment.get('speaker')}: " if segment.get("speaker") else ""
                sources.append(f"[{session['id']}#{index}] {speaker}{text}")
    source_text = "\n".join(sources)[:90_000]
    return (
        "Ты отвечаешь только по приведённым ниже источникам Omi Bridge. "
        "Не придумывай отсутствующие факты. Если ответа нет, скажи это прямо. "
        "Каждый существенный вывод снабди ссылкой на источник строго в формате "
        "[session_id#segment_index].\n\n"
        f"Вопрос пользователя: {question}\n\n"
        f"Источники:\n{source_text}"
    )


def ask_openai_compatible(base_url: str, model: str, api_key: str, prompt: str) -> str:
    request_body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=request_body,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ИИ-провайдер недоступен: {exc.reason}") from exc
    try:
        return safe_text(payload["choices"][0]["message"]["content"], 40_000)
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("ИИ-провайдер вернул неожиданный ответ") from exc


class BridgeStore:
    def __init__(self, database: str | Path, vault: str | Path):
        self.database = str(database)
        self.vault = Path(vault).resolve()
        self.vault.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.database)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connection() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    uid TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sample_rate INTEGER NOT NULL DEFAULT 16000,
                    transcript TEXT NOT NULL DEFAULT '',
                    segments_json TEXT NOT NULL DEFAULT '[]',
                    pcm_path TEXT NOT NULL,
                    wav_path TEXT,
                    audio_bytes INTEGER NOT NULL DEFAULT 0,
                    audio_chunks INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'capturing' CHECK(status IN ('capturing', 'finalized')),
                    mirror_status TEXT NOT NULL DEFAULT 'not_configured',
                    mirror_path TEXT,
                    mirror_error TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS session_search USING fts5(
                    session_id UNINDEXED, transcript
                );
                CREATE TABLE IF NOT EXISTS routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('clipboard', 'notes', 'ai_prompt')),
                    target TEXT,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'copied', 'exported')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS omi_memory_sources (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    omi_memory_id TEXT NOT NULL UNIQUE,
                    raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def setting(self, key: str, default: str = "") -> str:
        with self.connection() as con:
            value = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return value["value"] if value else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connection() as con:
            con.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def session_dir(self, session_id: str) -> Path:
        target = (self.vault / "sessions" / safe_session_id(session_id)).resolve()
        if self.vault not in target.parents:
            raise ValueError("Invalid session path")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def active_session(self, uid: str, requested_id: str = "", sample_rate: int = 16000) -> dict[str, Any]:
        uid = safe_text(uid, 240) or "local-user"
        session_id = safe_session_id(requested_id) if requested_id else ""
        with self.connection() as con:
            row = None
            if session_id:
                row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                row = con.execute(
                    "SELECT * FROM sessions WHERE uid = ? AND status = 'capturing' ORDER BY updated_at DESC LIMIT 1", (uid,)
                ).fetchone()
                if row:
                    updated = datetime.fromisoformat(row["updated_at"]).timestamp()
                    if time.time() - updated > 30:
                        row = None
            if row:
                return dict(row)
            session_id = session_id or safe_session_id(f"s-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}")
            pcm_path = self.session_dir(session_id) / "recording.pcm"
            payload = (session_id, uid, now_iso(), now_iso(), sample_rate, str(pcm_path))
            con.execute(
                """INSERT INTO sessions (id, uid, started_at, updated_at, sample_rate, pcm_path)
                   VALUES (?, ?, ?, ?, ?, ?)""", payload
            )
            return dict(con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())

    def append_audio(self, session: dict[str, Any], raw_pcm: bytes, sample_rate: int) -> dict[str, Any]:
        if sample_rate < 8_000 or sample_rate > 96_000:
            raise ValueError("sample_rate must be between 8000 and 96000")
        pcm_path = Path(session["pcm_path"])
        pcm_path.parent.mkdir(parents=True, exist_ok=True)
        with pcm_path.open("ab") as file:
            file.write(raw_pcm)
        with self.connection() as con:
            con.execute(
                """UPDATE sessions SET updated_at = ?, sample_rate = ?, audio_bytes = audio_bytes + ?,
                   audio_chunks = audio_chunks + 1, status = 'capturing', wav_path = NULL WHERE id = ?""",
                (now_iso(), sample_rate, len(raw_pcm), session["id"]),
            )
            return dict(con.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone())

    def append_segments(self, session: dict[str, Any], segments: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        valid = [
            {
                "text": safe_text(segment.get("text"), 4_000),
                "speaker": safe_text(segment.get("speaker_name") or segment.get("speaker"), 120),
                "is_user": bool(segment.get("is_user")),
                "start": segment.get("start"),
                "end": segment.get("end"),
            }
            for segment in segments
            if isinstance(segment, dict) and safe_text(segment.get("text"))
        ]
        previous = json.loads(session["segments_json"])
        known = {(item.get("text"), item.get("start"), item.get("end")) for item in previous}
        fresh = [item for item in valid if (item["text"], item["start"], item["end"]) not in known]
        joined = f"{session['transcript']}\n" + "\n".join(item["text"] for item in fresh)
        combined = previous + fresh
        routes = self._detect_routes(session["id"], fresh)
        with self.connection() as con:
            con.execute(
                "UPDATE sessions SET updated_at = ?, transcript = ?, segments_json = ? WHERE id = ?",
                (now_iso(), joined.strip(), json.dumps(combined, ensure_ascii=False), session["id"]),
            )
            con.execute("DELETE FROM session_search WHERE session_id = ?", (session["id"],))
            con.execute("INSERT INTO session_search (session_id, transcript) VALUES (?, ?)", (session["id"], joined.strip()))
            for route in routes:
                con.execute(
                    "INSERT INTO routes (session_id, kind, target, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (session["id"], route["kind"], route["target"], route["content"], now_iso()),
                )
            row = dict(con.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone())
        return row, routes

    def attach_omi_memory(self, session_id: str, payload: dict[str, Any]) -> None:
        memory_id = safe_text(payload.get("id"), 240)
        if not memory_id:
            raise ValueError("Omi memory payload must contain id")
        with self.connection() as con:
            con.execute(
                """INSERT INTO omi_memory_sources (session_id, omi_memory_id, raw_json) VALUES (?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET raw_json = excluded.raw_json""",
                (session_id, memory_id, json.dumps(payload, ensure_ascii=False)),
            )

    @staticmethod
    def _detect_routes(session_id: str, segments: list[dict[str, Any]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for segment in segments:
            match = ROUTE_RE.match(segment["text"])
            if not match:
                continue
            content = safe_text(match.group("content"), 10_000)
            if not content:
                continue
            if match.group("clipboard"):
                result.append({"kind": "clipboard", "target": "", "content": content})
            elif match.group("notes"):
                result.append({"kind": "notes", "target": "", "content": content})
            elif match.group("ai"):
                result.append({"kind": "ai_prompt", "target": safe_text(match.group("target"), 80), "content": content})
        return result

    def list_sessions(self, query: str = "", limit: int = 80, uid: str | None = None) -> list[dict[str, Any]]:
        query = safe_text(query, 240)
        uid = safe_text(uid, 240) if uid else ""
        with self.connection() as con:
            if query:
                words = re.findall(r"[\wа-яё-]+", query.lower(), re.IGNORECASE)
                fts = " AND ".join(f'"{word}"*' for word in words[:8])
                filters = ["session_search MATCH ?"]
                params: list[Any] = [fts]
                if uid:
                    filters.append("s.uid = ?")
                    params.append(uid)
                params.append(limit)
                try:
                    rows = con.execute(
                        """SELECT s.*, snippet(session_search, 1, '<mark>', '</mark>', '…', 20) AS excerpt
                           FROM session_search JOIN sessions s ON s.id = session_search.session_id
                           WHERE """ + " AND ".join(filters) + " ORDER BY s.updated_at DESC LIMIT ?",
                        params,
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            else:
                clause = "WHERE uid = ?" if uid else ""
                params = [uid, limit] if uid else [limit]
                rows = con.execute(
                    f"SELECT *, substr(transcript, 1, 220) AS excerpt FROM sessions {clause} ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
        return [self._session_summary(dict(row)) for row in rows]

    def get_session(self, session_id: str, uid: str | None = None) -> dict[str, Any] | None:
        uid = safe_text(uid, 240) if uid else ""
        with self.connection() as con:
            if uid:
                row = con.execute(
                    "SELECT * FROM sessions WHERE id = ? AND uid = ?", (safe_session_id(session_id), uid)
                ).fetchone()
            else:
                row = con.execute("SELECT * FROM sessions WHERE id = ?", (safe_session_id(session_id),)).fetchone()
            if not row:
                return None
            session = self._session_summary(dict(row))
            session["segments"] = json.loads(session.pop("segments_json"))
            session["routes"] = [dict(item) for item in con.execute(
                "SELECT * FROM routes WHERE session_id = ? ORDER BY id DESC", (session_id,)
            ).fetchall()]
            memory_source = con.execute("SELECT raw_json FROM omi_memory_sources WHERE session_id = ?", (session_id,)).fetchone()
            session["omi_memory"] = json.loads(memory_source["raw_json"]) if memory_source else None
            session["context"] = self.context_for(session)
            return session

    def _session_summary(self, session: dict[str, Any]) -> dict[str, Any]:
        session["wav_available"] = bool(session.get("wav_path") and Path(session["wav_path"]).is_file())
        session["audio_mb"] = round(session.get("audio_bytes", 0) / 1_000_000, 2)
        return session

    def context_for(self, session: dict[str, Any]) -> dict[str, Any]:
        segments = json.loads(session["segments_json"]) if isinstance(session.get("segments_json"), str) else session.get("segments", [])
        structured = session.get("omi_memory", {}).get("structured", {}) if isinstance(session.get("omi_memory"), dict) else {}
        structured = structured if isinstance(structured, dict) else {}
        participants: dict[str, list[int]] = {}
        entities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, segment in enumerate(segments):
            speaker = safe_text(segment.get("speaker"), 120)
            if speaker and not GENERIC_SPEAKER.match(speaker):
                participants.setdefault(speaker, []).append(index)
            text = safe_text(segment.get("text"), 4_000)
            for match in [*QUOTED_RE.finditer(text), *TAG_RE.finditer(text), *URL_RE.finditer(text)]:
                value = next((part for part in match.groups() if part), match.group(0))
                key = value.lower()
                if key not in seen:
                    seen.add(key)
                    entities.append({"value": value, "segment_index": index, "source": text[:280]})
        return {
            "schema": "omi-bridge-context/v1",
            "session_id": session["id"],
            "created_from": "local Omi live transcript",
            "participants": [{"name": name, "segment_indexes": indexes} for name, indexes in participants.items()],
            "explicit_entities": entities,
            "source_segments": [
                {"index": index, "text": segment.get("text", ""), "speaker": segment.get("speaker", ""), "start": segment.get("start"), "end": segment.get("end")}
                for index, segment in enumerate(segments)
            ],
            "omi_structured_source": (
                {
                    "title": safe_text(structured.get("title"), 500),
                    "overview": safe_text(structured.get("overview"), 4_000),
                    "action_items": structured.get("action_items", []),
                }
                if isinstance(session.get("omi_memory"), dict) else None
            ),
        }

    def finalize(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session:
            raise KeyError("Session not found")
        pcm_path = Path(session["pcm_path"])
        wav_path = self.session_dir(session_id) / "recording.wav"
        if pcm_path.is_file() and pcm_path.stat().st_size:
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(int(session["sample_rate"]))
                with pcm_path.open("rb") as input_file:
                    for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                        output.writeframesraw(chunk)
        with self.connection() as con:
            con.execute(
                "UPDATE sessions SET status = 'finalized', wav_path = ?, updated_at = ? WHERE id = ?",
                (str(wav_path) if wav_path.is_file() else None, now_iso(), session_id),
            )
        return self.get_session(session_id) or session

    def export(self, session_id: str, mirror: bool = True) -> dict[str, Any]:
        session = self.finalize(session_id)
        session_dir = self.session_dir(session_id)
        context = self.context_for(session)
        transcript_path = session_dir / "transcript.md"
        context_path = session_dir / "context.json"
        transcript_lines = [
            f"# Omi Bridge — {session['id']}",
            "",
            f"- Начало: {session['started_at']}",
            f"- Частота аудио: {session['sample_rate']} Hz PCM16 mono",
            f"- WAV: {Path(session['wav_path']).name if session.get('wav_path') else 'нет audio chunks'}",
            "",
            "## Транскрипт",
            "",
        ]
        for index, segment in enumerate(session["segments"]):
            label = f"[{segment.get('start', '?')}] " if segment.get("start") is not None else ""
            speaker = f"**{segment.get('speaker')}**: " if segment.get("speaker") else ""
            transcript_lines.append(f"{label}{speaker}{segment.get('text', '')}")
        transcript_path.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
        context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files = [transcript_path, context_path]
        if session.get("wav_path") and Path(session["wav_path"]).is_file():
            files.append(Path(session["wav_path"]))
        manifest_path = session_dir / "manifest.json"
        manifest_path.write_text(json.dumps({"session_id": session_id, "files": [{"name": item.name, "sha256": digest(item), "bytes": item.stat().st_size} for item in files]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files.append(manifest_path)
        result = {"session_id": session_id, "local_path": str(session_dir), "files": [item.name for item in files], "mirror": None}
        if mirror:
            result["mirror"] = self.mirror(session, files)
        return result

    def mirror(self, session: dict[str, Any], files: list[Path]) -> dict[str, Any]:
        root = self.setting("drive_mirror_root")
        if not root:
            return {"status": "not_configured"}
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            message = "Папка Google Drive не найдена. Проверь путь в Состоянии."
            with self.connection() as con:
                con.execute("UPDATE sessions SET mirror_status = 'error', mirror_error = ? WHERE id = ?", (message, session["id"]))
            return {"status": "error", "message": message}
        started = datetime.fromisoformat(session["started_at"])
        target = root_path / "Omi Bridge" / "Archive" / f"{started:%Y}" / f"{started:%m}" / session["id"]
        try:
            target.mkdir(parents=True, exist_ok=True)
            for source in files:
                temporary = target / f".{source.name}.partial"
                shutil.copy2(source, temporary)
                os.replace(temporary, target / source.name)
            with self.connection() as con:
                con.execute(
                    "UPDATE sessions SET mirror_status = 'mirrored', mirror_path = ?, mirror_error = NULL WHERE id = ?",
                    (str(target), session["id"]),
                )
            return {"status": "mirrored", "path": str(target)}
        except OSError as exc:
            with self.connection() as con:
                con.execute("UPDATE sessions SET mirror_status = 'error', mirror_error = ? WHERE id = ?", (str(exc), session["id"]))
            return {"status": "error", "message": str(exc)}

    def routes(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as con:
            rows = con.execute(
                """SELECT r.*, s.started_at, s.transcript FROM routes r JOIN sessions s ON s.id = r.session_id
                   ORDER BY r.id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def route(self, route_id: int) -> dict[str, Any] | None:
        with self.connection() as con:
            row = con.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        return dict(row) if row else None

    def update_route(self, route_id: int, status: str) -> bool:
        with self.connection() as con:
            cursor = con.execute("UPDATE routes SET status = ? WHERE id = ?", (status, route_id))
        return cursor.rowcount == 1

    def export_note(self, route_id: int) -> dict[str, str]:
        route = self.route(route_id)
        if not route:
            raise KeyError("Route not found")
        root = self.setting("notes_root")
        if not root or not Path(root).is_dir():
            raise ValueError("Папка заметок не настроена или недоступна")
        target_dir = Path(root).resolve() / "Omi Bridge Notes"
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        target = target_dir / f"{timestamp}_{route['session_id']}.md"
        temporary = target.with_suffix(".partial")
        temporary.write_text(
            f"# Голосовая заметка\n\n{route['content']}\n\n---\nИсточник: Omi Bridge session `{route['session_id']}`\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        self.update_route(route_id, "exported")
        return {"path": str(target), "status": "exported"}

    def status(self) -> dict[str, Any]:
        with self.connection() as con:
            total = con.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"]
            active = con.execute("SELECT COUNT(*) AS count FROM sessions WHERE status = 'capturing'").fetchone()["count"]
            routes = con.execute("SELECT COUNT(*) AS count FROM routes WHERE status = 'pending'").fetchone()["count"]
        return {
            "sessions": total,
            "active_sessions": active,
            "pending_routes": routes,
            "vault": str(self.vault),
            "mirror_root": self.setting("drive_mirror_root"),
        }


def create_app(
    database: str | Path | None = None,
    vault: str | Path | None = None,
    secret: str | None = None,
    allowed_uid: str | None = None,
) -> FastAPI:
    root = Path(__file__).resolve().parent
    store = BridgeStore(database or os.environ.get("OMI_BRIDGE_DB") or root / "omi_bridge.db", vault or os.environ.get("OMI_BRIDGE_VAULT") or root / "vault")
    store.initialize()
    webhook_secret = secret or os.environ.get("OMI_BRIDGE_SECRET") or DEFAULT_SECRET
    configured_owner_uid = safe_text(allowed_uid if allowed_uid is not None else os.environ.get("OMI_BRIDGE_ALLOWED_UID"), 240)
    public_base_url = safe_text(os.environ.get("OMI_BRIDGE_PUBLIC_URL"), 2_000).rstrip("/")
    dashboard_key = safe_text(os.environ.get("OMI_BRIDGE_DASHBOARD_KEY"), 240)
    app = FastAPI(title="Omi Field Relay", version="0.3.0")
    app.state.store = store
    app.state.webhook_secret = webhook_secret
    app.state.allowed_uid = configured_owner_uid
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

    def check_secret(candidate: str) -> None:
        if not hmac.compare_digest(candidate, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid Omi Bridge webhook secret")

    def check_owner(uid: str, *, allow_enrollment: bool = False) -> str:
        """Keep a private relay bound to one Omi account without storing an API token in the client."""
        candidate = safe_text(uid, 240)
        if not candidate:
            raise HTTPException(status_code=400, detail="Omi user id is required")
        owner = configured_owner_uid or store.setting("owner_uid")
        if not owner and allow_enrollment:
            store.set_setting("owner_uid", candidate)
            owner = candidate
        if not owner:
            raise HTTPException(status_code=409, detail="Relay owner is not paired yet. Complete one Omi conversation first.")
        if not hmac.compare_digest(candidate, owner):
            raise HTTPException(status_code=403, detail="This private relay belongs to a different Omi account")
        return candidate

    def endpoint_base(request: Request) -> str:
        return public_base_url or str(request.base_url).rstrip("/")

    @app.middleware("http")
    async def protect_remote_dashboard(request: Request, call_next: Any) -> Any:
        if (
            public_base_url
            and request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/webhooks/")
            and request.url.path != "/api/health"
        ):
            supplied = request.headers.get("X-Omi-Bridge-Key", "")
            if not dashboard_key or not hmac.compare_digest(supplied, dashboard_key):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
        return await call_next(request)

    def tool_body(body: Any, request: Request) -> tuple[str, dict[str, Any]]:
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Expected a tool request object")
        uid = safe_text(body.get("uid") or request.query_params.get("uid"), 240)
        args = body.get("args") or body.get("params") or body
        if not isinstance(args, dict):
            raise HTTPException(status_code=422, detail="Tool arguments must be an object")
        return check_owner(uid), args

    def tool_limit(value: Any, default: int = 5) -> int:
        try:
            return max(1, min(int(value or default), 8))
        except (TypeError, ValueError):
            return default

    def source_card(session: dict[str, Any]) -> str:
        context = session.get("context") or {}
        structured = context.get("omi_structured_source") or {}
        title = safe_text(structured.get("title"), 160) or session["id"]
        overview = safe_text(structured.get("overview"), 600)
        excerpt = safe_text(session.get("excerpt"), 500).replace("<mark>", "").replace("</mark>", "")
        return "\n".join(
            part
            for part in [
                f"• {title} — {session['id']}",
                f"  {overview}" if overview else "",
                f"  {excerpt}" if excerpt else "",
            ]
            if part
        )

    @app.get("/", response_class=HTMLResponse)
    def app_home() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html><html lang=\"ru\"><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>OMI FIELD//MEMORY</title>
            <style>body{margin:0;background:#111312;color:#e7e2d8;font:16px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}main{max-width:620px;margin:0 auto;padding:48px 24px}code{color:#ff5a36}p{color:#b5b7b6;line-height:1.55}.tag{font:12px ui-monospace,monospace;color:#9ba4a4;letter-spacing:.12em}</style></head><body><main><div class=\"tag\">PRIVATE OMI APP · FIELD RELAY</div><h1>Память с источниками.</h1><p>Приложение принимает только новые завершённые разговоры из Omi. Поиск, досье и Obsidian-черновики доступны в чате Omi.</p><p>Архив не публикуется и не передаётся в сторонние ИИ без отдельного действия.</p><p><code>STATUS: READY FOR PAIRING</code></p></main></body></html>"""
        )

    @app.get("/health")
    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "omi-field-relay"}

    @app.get("/setup")
    def setup(uid: str = "") -> dict[str, bool]:
        owner = configured_owner_uid or store.setting("owner_uid")
        return {"is_setup_completed": bool(uid) and (not owner or hmac.compare_digest(uid, owner))}

    @app.get("/.well-known/omi-tools.json")
    def chat_tools_manifest(request: Request) -> dict[str, Any]:
        base = endpoint_base(request)
        return {
            "setup_url": f"{base}/setup",
            "tools": [
                {
                    "name": "search_field_archive",
                    "description": "Search the user's private OMI FIELD//MEMORY archive. Return only source-grounded findings with session IDs.",
                    "endpoint": f"{base}/tools/search-field-archive",
                    "method": "POST",
                    "parameters": {
                        "properties": {
                            "query": {"type": "string", "description": "Words, person, project, link, or topic to find."},
                            "max_results": {"type": "integer", "description": "1 to 8 results; default 5."},
                        },
                        "required": ["query"],
                    },
                    "auth_required": False,
                    "status_message": "Searching FIELD//MEMORY…",
                },
                {
                    "name": "build_field_dossier",
                    "description": "Build a compact dossier or timeline from the user's captured Omi conversations. Distinguish source facts from unknowns.",
                    "endpoint": f"{base}/tools/build-field-dossier",
                    "method": "POST",
                    "parameters": {
                        "properties": {
                            "query": {"type": "string", "description": "Person, project, organization, place, or topic."},
                            "max_results": {"type": "integer", "description": "1 to 8 source conversations; default 5."},
                        },
                        "required": ["query"],
                    },
                    "auth_required": False,
                    "status_message": "Building a source-backed dossier…",
                },
                {
                    "name": "make_obsidian_draft",
                    "description": "Prepare a Markdown draft for Obsidian from source conversations. It does not write to external storage automatically.",
                    "endpoint": f"{base}/tools/make-obsidian-draft",
                    "method": "POST",
                    "parameters": {
                        "properties": {
                            "query": {"type": "string", "description": "Topic to export as an Obsidian-ready draft."},
                            "title": {"type": "string", "description": "Optional note title."},
                        },
                        "required": ["query"],
                    },
                    "auth_required": False,
                    "status_message": "Preparing an Obsidian draft…",
                },
                {
                    "name": "field_relay_status",
                    "description": "Show the capture status and number of saved source conversations for the current user.",
                    "endpoint": f"{base}/tools/field-relay-status",
                    "method": "POST",
                    "parameters": {"properties": {}, "required": []},
                    "auth_required": False,
                    "status_message": "Checking FIELD//MEMORY…",
                },
            ],
        }

    @app.post("/tools/search-field-archive")
    async def search_field_archive(request: Request) -> dict[str, str]:
        uid, args = tool_body(await request.json(), request)
        query = safe_text(args.get("query"), 240)
        if not query:
            raise HTTPException(status_code=422, detail="A search query is required")
        limit = tool_limit(args.get("max_results"))
        matches = store.list_sessions(query, limit, uid)
        if not matches:
            return {"result": "В FIELD//MEMORY нет подтверждённых совпадений по этому запросу."}
        cards = []
        for summary in matches:
            session = store.get_session(summary["id"], uid)
            if session:
                session["excerpt"] = summary.get("excerpt", "")
                cards.append(source_card(session))
        return {"result": "Найдены источники:\n" + "\n\n".join(cards)}

    @app.post("/tools/build-field-dossier")
    async def build_field_dossier(request: Request) -> dict[str, str]:
        uid, args = tool_body(await request.json(), request)
        query = safe_text(args.get("query"), 240)
        if not query:
            raise HTTPException(status_code=422, detail="A dossier topic is required")
        limit = tool_limit(args.get("max_results"))
        matches = store.list_sessions(query, limit, uid)
        if not matches:
            return {"result": "Досье не собрано: в архиве нет источников по этому запросу."}
        lines = [f"Досье: {query}", "", "Источники (в хронологическом порядке):"]
        for summary in reversed(matches):
            session = store.get_session(summary["id"], uid)
            if not session:
                continue
            context = session["context"]
            structured = context.get("omi_structured_source") or {}
            title = safe_text(structured.get("title"), 160) or session["id"]
            overview = safe_text(structured.get("overview"), 600) or safe_text(summary.get("excerpt"), 300)
            lines.extend([f"- {session['started_at']}: {title} [{session['id']}]", f"  {overview}"])
        lines.extend(["", "Неуверенные выводы и данные без источника не добавлены."])
        return {"result": "\n".join(lines)}

    @app.post("/tools/make-obsidian-draft")
    async def make_obsidian_draft(request: Request) -> dict[str, str]:
        uid, args = tool_body(await request.json(), request)
        query = safe_text(args.get("query"), 240)
        if not query:
            raise HTTPException(status_code=422, detail="An Obsidian note topic is required")
        title = safe_text(args.get("title"), 160) or query
        matches = store.list_sessions(query, 5, uid)
        if not matches:
            return {"result": "Черновик не создан: в архиве нет источников по этому запросу."}
        lines = [f"# {title}", "", f"_Источник: OMI FIELD//MEMORY · запрос «{query}»_", "", "## Источники"]
        for summary in matches:
            session = store.get_session(summary["id"], uid)
            if not session:
                continue
            structured = session["context"].get("omi_structured_source") or {}
            overview = safe_text(structured.get("overview"), 900) or safe_text(summary.get("excerpt"), 500)
            lines.extend([f"- [[{session['id']}]] — {overview}"])
        lines.extend(["", "## Проверить", "", "- [ ] Сверить выводы с указанными исходными сессиями перед публикацией или отправкой."])
        return {"result": "\n".join(lines)}

    @app.post("/tools/field-relay-status")
    async def field_relay_status(request: Request) -> dict[str, str]:
        uid, _ = tool_body(await request.json(), request)
        saved = store.list_sessions(limit=1_000, uid=uid)
        return {"result": f"FIELD//MEMORY готов. В личном архиве: {len(saved)} сессий. Новые завершённые разговоры будут добавляться автоматически."}


    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {**store.status(), "default_secret": hmac.compare_digest(webhook_secret, DEFAULT_SECRET)}

    @app.get("/api/ai/status")
    def ai_status() -> dict[str, Any]:
        base_url = os.environ.get("OMI_BRIDGE_AI_BASE_URL", "")
        return {
            "configured": bool(base_url and os.environ.get("OMI_BRIDGE_AI_MODEL")),
            "base_url": base_url,
            "model": os.environ.get("OMI_BRIDGE_AI_MODEL", ""),
            "key_from_environment": bool(os.environ.get("OMI_BRIDGE_AI_KEY")),
        }

    @app.post("/api/ask")
    async def ask_ai(request: Request) -> dict[str, Any]:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="Expected an ask request")
        question = safe_text(body.get("question"), 10_000)
        session_ids = body.get("session_ids")
        execute = bool(body.get("execute"))
        if not question:
            raise HTTPException(status_code=422, detail="Нужен вопрос")
        if not isinstance(session_ids, list) or not session_ids:
            raise HTTPException(status_code=422, detail="Выбери хотя бы одну сессию для контекста")
        sessions_for_context = []
        for session_id in session_ids[:8]:
            session = store.get_session(safe_text(session_id, 120))
            if session:
                sessions_for_context.append(session)
        if not sessions_for_context:
            raise HTTPException(status_code=404, detail="Выбранные сессии не найдены")
        prompt = build_context_prompt(question, sessions_for_context)
        result: dict[str, Any] = {
            "prompt": prompt,
            "source_count": sum(len(session["segments"]) for session in sessions_for_context),
            "sent": False,
        }
        if not execute:
            return result
        base_url = os.environ.get("OMI_BRIDGE_AI_BASE_URL", "")
        model = os.environ.get("OMI_BRIDGE_AI_MODEL", "")
        if not base_url or not model:
            raise HTTPException(status_code=409, detail="ИИ-провайдер не настроен. Скопируй prompt или задай OMI_BRIDGE_AI_BASE_URL и OMI_BRIDGE_AI_MODEL.")
        try:
            result["answer"] = ask_openai_compatible(base_url, model, os.environ.get("OMI_BRIDGE_AI_KEY", ""), prompt)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        result["sent"] = True
        return result

    @app.get("/api/sessions")
    def sessions(q: str = "") -> list[dict[str, Any]]:
        return store.list_sessions(q)

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str) -> dict[str, Any]:
        result = store.get_session(session_id)
        if not result:
            raise HTTPException(status_code=404, detail="Session not found")
        return result

    @app.post("/api/sessions/{session_id}/finalize")
    def finalize(session_id: str) -> dict[str, Any]:
        try:
            return store.finalize(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    @app.post("/api/sessions/{session_id}/export")
    def export(session_id: str) -> dict[str, Any]:
        try:
            return store.export(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    @app.get("/api/routes")
    def routes() -> list[dict[str, Any]]:
        return store.routes()

    @app.get("/api/routes/{route_id}/prompt")
    def route_prompt(route_id: int) -> dict[str, str]:
        route = store.route(route_id)
        if not route:
            raise HTTPException(status_code=404, detail="Route not found")
        target = route["target"] or "ИИ"
        return {"text": f"Задача для {target}:\n\n{route['content']}\n\nИсточник: голосовая сессия Omi Bridge {route['session_id']}.\nРаботай только с этим контекстом; если данных не хватает — задай уточняющий вопрос."}

    @app.post("/api/routes/{route_id}/copied")
    def route_copied(route_id: int) -> dict[str, str]:
        if not store.update_route(route_id, "copied"):
            raise HTTPException(status_code=404, detail="Route not found")
        return {"status": "copied"}

    @app.post("/api/routes/{route_id}/export-note")
    def route_export_note(route_id: int) -> dict[str, str]:
        try:
            return store.export_note(route_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Route not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/settings")
    def settings() -> dict[str, str]:
        return {"drive_mirror_root": store.setting("drive_mirror_root"), "notes_root": store.setting("notes_root")}

    @app.put("/api/settings/drive-mirror")
    async def set_drive_mirror(request: Request) -> dict[str, str]:
        body = await request.json()
        path = safe_text(body.get("path") if isinstance(body, dict) else "", 2_000)
        if path and not Path(path).expanduser().is_dir():
            raise HTTPException(status_code=422, detail="Указанная папка не найдена")
        store.set_setting("drive_mirror_root", str(Path(path).expanduser().resolve()) if path else "")
        return {"drive_mirror_root": store.setting("drive_mirror_root")}

    @app.put("/api/settings/notes-root")
    async def set_notes_root(request: Request) -> dict[str, str]:
        body = await request.json()
        path = safe_text(body.get("path") if isinstance(body, dict) else "", 2_000)
        if path and not Path(path).expanduser().is_dir():
            raise HTTPException(status_code=422, detail="Указанная папка не найдена")
        store.set_setting("notes_root", str(Path(path).expanduser().resolve()) if path else "")
        return {"notes_root": store.setting("notes_root")}

    @app.get("/api/setup")
    def setup() -> dict[str, Any]:
        return {
            "memory_path": "/api/webhooks/omi/{secret}/memory",
            "live_path": "/api/webhooks/omi/{secret}/live?uid={uid}&session_id={session_id}",
            "audio_path": "/api/webhooks/omi/{secret}/audio?uid={uid}&session_id={session_id}&sample_rate=16000",
            "requires_https": True,
            "uses_default_secret": hmac.compare_digest(webhook_secret, DEFAULT_SECRET),
        }

    @app.post("/api/demo/seed")
    def seed_demo() -> dict[str, Any]:
        """Create a clearly local-only data set for first-run UI testing."""
        session = store.active_session("demo-local", "demo-local-capture", 16000)
        session, routes = store.append_segments(session, [
            {"text": "Обсудим запуск новой версии на следующей неделе.", "speaker_name": "Иван", "start": 1.2},
            {"text": "в буфер: Согласовать срок с командой", "speaker_name": "Ты", "start": 5.4},
            {"text": "запрос ChatGPT: Составь план запуска только по этому разговору", "speaker_name": "Ты", "start": 9.0},
        ])
        if not session["audio_bytes"]:
            session = store.append_audio(session, b"\x00\x00\x10\x00" * 1600, 16000)
        return {"session_id": session["id"], "routes": routes, "local_only": True}

    @app.post("/api/webhooks/omi/{candidate_secret}/live")
    async def receive_live(candidate_secret: str, request: Request, uid: str = "local-user", session_id: str = "") -> dict[str, Any]:
        check_secret(candidate_secret)
        uid = check_owner(uid, allow_enrollment=True)
        payload = await request.json()
        segments = payload.get("segments", []) if isinstance(payload, dict) else payload
        if not isinstance(segments, list):
            raise HTTPException(status_code=422, detail="Expected a segment array")
        session = store.active_session(uid, session_id)
        updated, detected_routes = store.append_segments(session, segments)
        return {"session_id": updated["id"], "accepted_segments": len(segments), "routes": detected_routes}

    @app.post("/api/webhooks/omi/{candidate_secret}/memory")
    async def receive_memory(candidate_secret: str, request: Request, uid: str = "local-user") -> dict[str, Any]:
        check_secret(candidate_secret)
        uid = check_owner(uid, allow_enrollment=True)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Expected an Omi memory object")
        if payload.get("discarded"):
            return {"status": "ignored", "reason": "discarded"}
        memory_id = safe_text(payload.get("id"), 240)
        if not memory_id:
            raise HTTPException(status_code=422, detail="Omi memory payload must contain id")
        segments = payload.get("transcript_segments") if isinstance(payload.get("transcript_segments"), list) else []
        session = store.active_session(uid, f"omi-{memory_id}")
        updated, detected_routes = store.append_segments(session, segments)
        try:
            store.attach_omi_memory(updated["id"], payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "stored", "session_id": updated["id"], "routes": detected_routes}

    @app.post("/api/webhooks/omi/{candidate_secret}/audio")
    async def receive_audio(candidate_secret: str, request: Request, uid: str = "local-user", session_id: str = "", sample_rate: int = 16000) -> dict[str, Any]:
        check_secret(candidate_secret)
        uid = check_owner(uid, allow_enrollment=True)
        raw_pcm = await request.body()
        if not raw_pcm:
            raise HTTPException(status_code=422, detail="Expected a PCM16 request body")
        if len(raw_pcm) % 2:
            raise HTTPException(status_code=422, detail="PCM16 body must contain whole 16-bit samples")
        session = store.active_session(uid, session_id, sample_rate)
        updated = store.append_audio(session, raw_pcm, sample_rate)
        return {"session_id": updated["id"], "audio_chunks": updated["audio_chunks"], "audio_bytes": updated["audio_bytes"]}

    return app


app = create_app()
