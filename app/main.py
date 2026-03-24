from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import get_settings
from app.engines import GenerationEngineError, get_engine

DB_PATH = Path(__file__).resolve().parent.parent / "eixo.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def deterministic_key(collection_code: str, track_code: str, volume_code: str, chapter_code: str, piece_kind: str, syllabus_checksum: str, request_mode: str) -> str:
    mode = "create_if_missing" if request_mode == "force_new_version" else request_mode
    return "::".join([collection_code, track_code, volume_code or "-", chapter_code or "-", piece_kind, syllabus_checksum, mode])


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS collections (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT, description TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS tracks (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER, code TEXT, name TEXT, description TEXT, canonical_order INTEGER, status TEXT, is_active INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS volumes (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER, track_id INTEGER, code TEXT, title TEXT, summary TEXT, canonical_order INTEGER, status TEXT, source_master_ref TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS chapters (id INTEGER PRIMARY KEY AUTOINCREMENT, volume_id INTEGER, code TEXT, title TEXT, summary TEXT, canonical_order INTEGER, status TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS syllabi (id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INTEGER, track_id INTEGER, volume_id INTEGER, chapter_id INTEGER, version TEXT, status TEXT, title TEXT, objectives_json TEXT, program_content_json TEXT, notes_json TEXT, source_master_ref TEXT, checksum TEXT, created_at TEXT, updated_at TEXT);

            CREATE TABLE IF NOT EXISTS generation_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, collection_id INTEGER, track_id INTEGER, volume_id INTEGER, chapter_id INTEGER,
                piece_kind TEXT, request_mode TEXT, prompt_context_json TEXT, syllabus_id INTEGER, syllabus_checksum TEXT,
                status TEXT, generation_key TEXT,
                execution_mode TEXT, provider_name TEXT, model_name TEXT, failure_reason TEXT, attempt_count INTEGER DEFAULT 0, started_at TEXT,
                created_at TEXT, updated_at TEXT, completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS generated_pieces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER, track_id INTEGER, volume_id INTEGER, chapter_id INTEGER,
                syllabus_id INTEGER, syllabus_checksum TEXT, piece_kind TEXT, title TEXT, version INTEGER,
                status TEXT, review_state TEXT, is_current INTEGER,
                generation_request_id INTEGER,
                content_markdown TEXT, content_plaintext TEXT, storage_path TEXT,
                origin TEXT, created_by_user_id INTEGER,
                generator_provider TEXT, generator_model TEXT, generation_trace_json TEXT, source_request_payload_json TEXT, content_hash TEXT,
                supersedes_piece_id INTEGER, superseded_by_piece_id INTEGER,
                reviewer_user_id INTEGER, review_notes_text TEXT,
                approved_at TEXT, rejected_at TEXT, archived_at TEXT,
                created_at TEXT, updated_at TEXT, published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS piece_reuse_index (id INTEGER PRIMARY KEY AUTOINCREMENT, generation_key TEXT UNIQUE, generated_piece_id INTEGER, is_active INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS user_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, generated_piece_id INTEGER, collection_id INTEGER, track_id INTEGER, volume_id INTEGER, chapter_id INTEGER,
                progress_percent REAL, state TEXT,
                journey_state TEXT, resume_anchor_json TEXT,
                started_at TEXT, last_accessed_at TEXT, completed_at TEXT,
                last_meaningful_action_at TEXT, review_recommended_at TEXT, likely_abandoned_at TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, generated_piece_id INTEGER, volume_id INTEGER, chapter_id INTEGER, note_type TEXT, content TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS piece_access_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, generated_piece_id INTEGER, access_type TEXT, created_at TEXT, metadata_json TEXT);
            CREATE TABLE IF NOT EXISTS piece_review_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, generated_piece_id INTEGER, decided_by_user_id INTEGER, decision_type TEXT, decision_notes TEXT, checklist_json TEXT, created_at TEXT);

            CREATE TABLE IF NOT EXISTS user_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                recommendation_type TEXT,
                target_piece_id INTEGER,
                target_volume_id INTEGER,
                target_chapter_id INTEGER,
                reason_code TEXT,
                reason_text TEXT,
                priority_rank INTEGER,
                is_active INTEGER,
                created_at TEXT,
                updated_at TEXT,
                dismissed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS curriculum_import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT,
                source_name TEXT,
                source_hash TEXT,
                imported_by_user_id INTEGER,
                status TEXT,
                payload_json TEXT,
                preview_json TEXT,
                validation_report_json TEXT,
                applied_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS curriculum_import_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_batch_id INTEGER,
                issue_type TEXT,
                severity TEXT,
                target_kind TEXT,
                target_ref TEXT,
                message TEXT,
                details_json TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS curriculum_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_batch_id INTEGER,
                collection_id INTEGER,
                version_label TEXT,
                checksum TEXT,
                applied_by_user_id INTEGER,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS editorial_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_kind TEXT,
                target_kind TEXT,
                target_ref TEXT,
                related_generation_request_id INTEGER,
                related_generated_piece_id INTEGER,
                created_by_user_id INTEGER,
                assigned_to_user_id INTEGER,
                priority TEXT,
                status TEXT,
                dedupe_key TEXT,
                payload_json TEXT,
                result_json TEXT,
                failure_reason TEXT,
                retry_count INTEGER,
                max_retries INTEGER,
                available_at TEXT,
                locked_at TEXT,
                completed_at TEXT,
                cancelled_at TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS editorial_batch_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_kind TEXT,
                created_by_user_id INTEGER,
                status TEXT,
                filters_json TEXT,
                target_ids_json TEXT,
                result_summary_json TEXT,
                created_at TEXT,
                updated_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS job_execution_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                editorial_job_id INTEGER,
                event_type TEXT,
                message TEXT,
                details_json TEXT,
                created_at TEXT
            );
            """
        )


def seed_data() -> None:
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) c FROM collections").fetchone()["c"]:
            return
        now = utc_now()
        conn.execute("INSERT INTO collections (code,name,description,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?)", ("LAB_A_ROCHA", "Coleção Rocha", "Formação técnica EIXO", 1, now, now))
        collection_id = conn.execute("SELECT id FROM collections WHERE code='LAB_A_ROCHA'").fetchone()["id"]
        conn.execute("INSERT INTO tracks (collection_id,code,name,description,canonical_order,status,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (collection_id, "EMBARCADOS", "Trilha Embarcados", "Base em sistemas embarcados", 1, "active", 1, now, now))
        track_id = conn.execute("SELECT id FROM tracks WHERE code='EMBARCADOS'").fetchone()["id"]
        conn.execute("INSERT INTO volumes (collection_id,track_id,code,title,summary,canonical_order,status,source_master_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (collection_id, track_id, "V1", "Volume 1", "Fundamentos", 1, "authorized", "master:v1", now, now))
        volume_id = conn.execute("SELECT id FROM volumes WHERE code='V1'").fetchone()["id"]
        conn.execute("INSERT INTO chapters (volume_id,code,title,summary,canonical_order,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (volume_id, "C1", "Capítulo 1", "Introdução", 1, "authorized", now, now))
        conn.execute("INSERT INTO chapters (volume_id,code,title,summary,canonical_order,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (volume_id, "C2", "Capítulo 2", "Avançando", 2, "authorized", now, now))
        chapter_id = conn.execute("SELECT id FROM chapters WHERE code='C1'").fetchone()["id"]
        checksum = hashlib.sha256(b"ementa_v1").hexdigest()
        conn.execute("INSERT INTO syllabi (collection_id,track_id,volume_id,chapter_id,version,status,title,objectives_json,program_content_json,notes_json,source_master_ref,checksum,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (collection_id, track_id, volume_id, chapter_id, "1.0.0", "active", "Ementa C1", json.dumps(["obj"]), json.dumps(["pc"]), json.dumps([]), "master:v1", checksum, now, now))
        chapter2_id = conn.execute("SELECT id FROM chapters WHERE code='C2'").fetchone()["id"]
        checksum2 = hashlib.sha256(b"ementa_v2").hexdigest()
        conn.execute("INSERT INTO syllabi (collection_id,track_id,volume_id,chapter_id,version,status,title,objectives_json,program_content_json,notes_json,source_master_ref,checksum,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (collection_id, track_id, volume_id, chapter2_id, "1.0.0", "active", "Ementa C2", json.dumps(["obj2"]), json.dumps(["pc2"]), json.dumps([]), "master:v1", checksum2, now, now))


class GenerationRequestIn(BaseModel):
    user_id: int
    collection_id: int
    track_id: int
    volume_id: int | None = None
    chapter_id: int | None = None
    piece_kind: str
    request_mode: str = "create_if_missing"
    prompt_context_json: dict[str, Any] = {}


class ReviewIn(BaseModel):
    decided_by_user_id: int
    decision_type: str
    decision_notes: str = ""
    checklist: dict[str, Any] = {}


class ProgressIn(BaseModel):
    user_id: int
    generated_piece_id: int | None = None
    collection_id: int
    track_id: int
    volume_id: int | None = None
    chapter_id: int | None = None
    progress_percent: float
    state: str


class NoteIn(BaseModel):
    user_id: int
    generated_piece_id: int | None = None
    volume_id: int | None = None
    chapter_id: int | None = None
    note_type: str
    content: str


class AccessLogIn(BaseModel):
    user_id: int
    access_type: str
    metadata_json: dict[str, Any] = {}


class ImportSyllabusIn(BaseModel):
    collection_id: int
    track_id: int
    volume_id: int
    chapter_id: int | None = None
    version: str
    title: str
    objectives_json: list[str]
    program_content_json: list[str]
    notes_json: list[str] = []
    source_master_ref: str


class ReviewStateIn(BaseModel):
    review_state: str


app = FastAPI(title="EIXO Formação / EIXO Trilhas")


@app.on_event("startup")
def setup() -> None:
    init_db()
    seed_data()


def get_current_syllabus(conn: sqlite3.Connection, volume_id: int | None, chapter_id: int | None):
    if chapter_id:
        return conn.execute("SELECT * FROM syllabi WHERE chapter_id=? AND status='active' ORDER BY id DESC LIMIT 1", (chapter_id,)).fetchone()
    if volume_id:
        return conn.execute("SELECT * FROM syllabi WHERE volume_id=? AND chapter_id IS NULL AND status='active' ORDER BY id DESC LIMIT 1", (volume_id,)).fetchone()
    return None


def check_authorized(conn: sqlite3.Connection, volume_id: int | None, chapter_id: int | None) -> None:
    if chapter_id:
        ch = conn.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        if not ch or ch["status"] != "authorized":
            raise HTTPException(status_code=400, detail="Chapter não autorizado")
    elif volume_id:
        v = conn.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
        if not v or v["status"] != "authorized":
            raise HTTPException(status_code=400, detail="Volume não autorizado")
    else:
        raise HTTPException(status_code=400, detail="Volume ou chapter obrigatório")


def generation_blocks() -> list[dict[str, str]]:
    return [{"kind": "abertura", "title": "Abertura", "objective": "contextualizar"}, {"kind": "desenvolvimento", "title": "Desenvolvimento", "objective": "explicar"}, {"kind": "fechamento", "title": "Fechamento", "objective": "sintetizar"}]


def stitch_blocks(blocks: list[dict[str, Any]], title: str) -> str:
    txt = "\n\n".join([b["normalized_content_markdown"].strip() for b in blocks if b.get("normalized_content_markdown", "").strip()])
    if not txt:
        raise GenerationEngineError("Peça final vazia")
    return f"# {title}\n\n{txt}"


def editorial_rank(state: str) -> int:
    return {"approved": 3, "reviewed": 2, "raw": 1}.get(state, 0)


def log_review(conn: sqlite3.Connection, piece_id: int, user_id: int, dt: str, notes: str, checklist: dict[str, Any]) -> None:
    conn.execute("INSERT INTO piece_review_decisions (generated_piece_id,decided_by_user_id,decision_type,decision_notes,checklist_json,created_at) VALUES (?,?,?,?,?,?)", (piece_id, user_id, dt, notes, json.dumps(checklist), utc_now()))


def valid_piece_for_user(piece: sqlite3.Row) -> bool:
    return piece and piece["status"] == "ready" and piece["review_state"] != "rejected" and piece["status"] != "archived"


def compute_journey_state(progress: sqlite3.Row, settings) -> tuple[str, str | None, str | None]:
    now = datetime.now(timezone.utc)
    la = parse_dt(progress["last_accessed_at"])
    journey = "not_started"
    likely_abandoned_at = None
    review_recommended_at = None
    if progress["state"] == "completed":
        journey = "completed"
    elif progress["state"] == "in_progress":
        if la and now - la > timedelta(days=settings.journey_abandoned_after_days):
            journey = "likely_abandoned"
            likely_abandoned_at = utc_now()
        elif la and now - la > timedelta(hours=settings.journey_paused_after_hours):
            journey = "paused"
        else:
            journey = "in_progress"
    if progress["state"] == "completed" and la and now - la > timedelta(days=settings.review_suggest_after_days):
        journey = "review_recommended"
        review_recommended_at = utc_now()
    return journey, review_recommended_at, likely_abandoned_at


def compute_recommendations(conn: sqlite3.Connection, user_id: int, persist: bool = True) -> list[dict[str, Any]]:
    settings = get_settings()
    recs: list[dict[str, Any]] = []
    progresses = conn.execute("SELECT * FROM user_progress WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()

    for p in progresses:
        piece = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (p["generated_piece_id"],)).fetchone() if p["generated_piece_id"] else None
        if piece and not valid_piece_for_user(piece):
            continue
        journey, rr_at, la_at = compute_journey_state(p, settings)
        anchor = {"generated_piece_id": p["generated_piece_id"], "chapter_id": p["chapter_id"], "action": "resume" if journey in ["in_progress", "paused", "likely_abandoned"] else "review"}
        conn.execute("UPDATE user_progress SET journey_state=?,resume_anchor_json=?,review_recommended_at=?,likely_abandoned_at=?,updated_at=? WHERE id=?", (journey, json.dumps(anchor), rr_at, la_at, utc_now(), p["id"]))

        if journey in ["in_progress", "paused", "likely_abandoned"]:
            reason = "Você parou nesta peça antes de concluir."
            code = "resume_in_progress" if journey == "in_progress" else ("paused_window" if journey == "paused" else "likely_abandoned_window")
            recs.append({"recommendation_type": "resume_piece", "target_piece_id": p["generated_piece_id"], "target_volume_id": p["volume_id"], "target_chapter_id": p["chapter_id"], "reason_code": code, "reason_text": reason, "priority_rank": 1 if journey == "in_progress" else (2 if journey == "paused" else 3)})

        if journey == "review_recommended":
            has_doubt = conn.execute("SELECT 1 FROM user_notes WHERE user_id=? AND generated_piece_id=? AND note_type='duvida' LIMIT 1", (user_id, p["generated_piece_id"])).fetchone()
            if has_doubt:
                recs.append({"recommendation_type": "review_piece", "target_piece_id": p["generated_piece_id"], "target_volume_id": p["volume_id"], "target_chapter_id": p["chapter_id"], "reason_code": "note_doubt", "reason_text": "Há uma dúvida registrada aqui; vale revisar este ponto.", "priority_rank": 2})

        if p["state"] == "completed" and p["progress_percent"] >= 80 and p["chapter_id"]:
            current_ch = conn.execute("SELECT * FROM chapters WHERE id=?", (p["chapter_id"],)).fetchone()
            nxt = conn.execute("SELECT * FROM chapters WHERE volume_id=? AND canonical_order>? AND status='authorized' ORDER BY canonical_order LIMIT 1", (current_ch["volume_id"], current_ch["canonical_order"])).fetchone()
            if nxt:
                nxt_piece = conn.execute("SELECT * FROM generated_pieces WHERE chapter_id=? AND is_current=1", (nxt["id"],)).fetchone()
                if nxt_piece and valid_piece_for_user(nxt_piece):
                    recs.append({"recommendation_type": "continue_track", "target_piece_id": nxt_piece["id"], "target_volume_id": None, "target_chapter_id": nxt["id"], "reason_code": "next_chapter_ready", "reason_text": "O capítulo seguinte já está liberado.", "priority_rank": 4})
                else:
                    recs.append({"recommendation_type": "generate_missing_piece", "target_piece_id": None, "target_volume_id": current_ch["volume_id"], "target_chapter_id": nxt["id"], "reason_code": "missing_current_piece", "reason_text": "Ainda não existe peça salva para o próximo passo.", "priority_rank": 5})

    recs = sorted(recs, key=lambda x: x["priority_rank"])[:10]
    if persist:
        conn.execute("UPDATE user_recommendations SET is_active=0,updated_at=? WHERE user_id=? AND is_active=1", (utc_now(), user_id))
        for r in recs:
            conn.execute("INSERT INTO user_recommendations (user_id,recommendation_type,target_piece_id,target_volume_id,target_chapter_id,reason_code,reason_text,priority_rank,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (user_id, r["recommendation_type"], r["target_piece_id"], r["target_volume_id"], r["target_chapter_id"], r["reason_code"], r["reason_text"], r["priority_rank"], 1, utc_now(), utc_now()))
    return recs


def set_single_current(conn: sqlite3.Connection, piece: sqlite3.Row) -> None:
    conn.execute("UPDATE generated_pieces SET is_current=0,updated_at=? WHERE track_id=? AND IFNULL(volume_id,-1)=IFNULL(?,-1) AND IFNULL(chapter_id,-1)=IFNULL(?,-1) AND piece_kind=? AND syllabus_checksum=?", (utc_now(), piece["track_id"], piece["volume_id"], piece["chapter_id"], piece["piece_kind"], piece["syllabus_checksum"]))
    conn.execute("UPDATE generated_pieces SET is_current=1,updated_at=? WHERE id=?", (utc_now(), piece["id"]))


# curricular
@app.get('/api/v1/collections')
def collections():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM collections WHERE is_active=1").fetchall()]


@app.get('/api/v1/collections/{collection_id}/tracks')
def tracks(collection_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tracks WHERE collection_id=? AND is_active=1 ORDER BY canonical_order", (collection_id,)).fetchall()]


@app.get('/api/v1/tracks/{track_id}/volumes')
def volumes(track_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM volumes WHERE track_id=? AND status='authorized' ORDER BY canonical_order", (track_id,)).fetchall()]


@app.get('/api/v1/volumes/{volume_id}')
def volume(volume_id: int):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Volume não encontrado")
        return dict(r)


@app.get('/api/v1/chapters/{chapter_id}')
def chapter(chapter_id: int):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Chapter não encontrado")
        return dict(r)


@app.get('/api/v1/volumes/{volume_id}/syllabus/current')
def syllabus_v(volume_id: int):
    with get_conn() as conn:
        s = get_current_syllabus(conn, volume_id, None)
        if not s:
            raise HTTPException(404, "Sem ementa vigente")
        return dict(s)


@app.get('/api/v1/chapters/{chapter_id}/syllabus/current')
def syllabus_c(chapter_id: int):
    with get_conn() as conn:
        s = get_current_syllabus(conn, None, chapter_id)
        if not s:
            raise HTTPException(404, "Sem ementa vigente")
        return dict(s)


# generation
def _gen_request_with_conn(conn: sqlite3.Connection, payload: GenerationRequestIn):
    settings = get_settings()
    check_authorized(conn, payload.volume_id, payload.chapter_id)
    s = get_current_syllabus(conn, payload.volume_id, payload.chapter_id)
    if not s:
        raise HTTPException(400, "Geração depende de ementa vigente válida")
    c = conn.execute("SELECT * FROM collections WHERE id=?", (payload.collection_id,)).fetchone()
    t = conn.execute("SELECT * FROM tracks WHERE id=?", (payload.track_id,)).fetchone()
    volume_code = conn.execute("SELECT code FROM volumes WHERE id=?", (payload.volume_id,)).fetchone()["code"] if payload.volume_id else "-"
    chapter_code = conn.execute("SELECT code FROM chapters WHERE id=?", (payload.chapter_id,)).fetchone()["code"] if payload.chapter_id else "-"
    gk = deterministic_key(c["code"], t["code"], volume_code, chapter_code, payload.piece_kind, s["checksum"], payload.request_mode)

    if payload.request_mode != 'force_new_version':
        cands = conn.execute("SELECT gp.* FROM generated_pieces gp JOIN piece_reuse_index pri ON pri.generated_piece_id=gp.id WHERE pri.generation_key=? AND pri.is_active=1 AND gp.is_current=1 AND gp.status='ready'", (gk,)).fetchall()
        if cands:
            chosen = sorted(cands, key=lambda x: editorial_rank(x['review_state']), reverse=True)[0]
            req_id = conn.execute("INSERT INTO generation_requests (user_id,collection_id,track_id,volume_id,chapter_id,piece_kind,request_mode,prompt_context_json,syllabus_id,syllabus_checksum,status,generation_key,execution_mode,provider_name,model_name,attempt_count,created_at,updated_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (payload.user_id, payload.collection_id, payload.track_id, payload.volume_id, payload.chapter_id, payload.piece_kind, payload.request_mode, json.dumps(payload.prompt_context_json), s['id'], s['checksum'], 'reused', gk, 'reused', chosen['generator_provider'], chosen['generator_model'], 0, utc_now(), utc_now(), utc_now())).lastrowid
            return {'request_id': req_id, 'status': 'reused', 'generated_piece': dict(chosen)}

    req_id = conn.execute("INSERT INTO generation_requests (user_id,collection_id,track_id,volume_id,chapter_id,piece_kind,request_mode,prompt_context_json,syllabus_id,syllabus_checksum,status,generation_key,execution_mode,attempt_count,started_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (payload.user_id, payload.collection_id, payload.track_id, payload.volume_id, payload.chapter_id, payload.piece_kind, payload.request_mode, json.dumps(payload.prompt_context_json), s['id'], s['checksum'], 'processing', gk, settings.generation_mode, 0, utc_now(), utc_now(), utc_now())).lastrowid
    try:
        blocks = [b.__dict__ for b in get_engine(settings).generate_piece({"program_content": ["pc"], "blocks": generation_blocks(), "target": {}, "syllabus": {}, "style_rules": [], "forbidden": []})]
        title = f"{payload.piece_kind.upper()} {volume_code}/{chapter_code}"
        content = stitch_blocks(blocks, title)
        previous = conn.execute("SELECT * FROM generated_pieces WHERE track_id=? AND IFNULL(volume_id,-1)=IFNULL(?,-1) AND IFNULL(chapter_id,-1)=IFNULL(?,-1) AND piece_kind=? AND syllabus_checksum=? AND is_current=1 ORDER BY id DESC LIMIT 1", (payload.track_id, payload.volume_id, payload.chapter_id, payload.piece_kind, s['checksum'])).fetchone()
        version = conn.execute("SELECT COALESCE(MAX(version),0) v FROM generated_pieces WHERE track_id=? AND IFNULL(volume_id,-1)=IFNULL(?,-1) AND IFNULL(chapter_id,-1)=IFNULL(?,-1) AND piece_kind=?", (payload.track_id, payload.volume_id, payload.chapter_id, payload.piece_kind)).fetchone()['v'] + 1
        pid = conn.execute("INSERT INTO generated_pieces (collection_id,track_id,volume_id,chapter_id,syllabus_id,syllabus_checksum,piece_kind,title,version,status,review_state,is_current,generation_request_id,content_markdown,content_plaintext,storage_path,origin,created_by_user_id,generator_provider,generator_model,generation_trace_json,source_request_payload_json,content_hash,supersedes_piece_id,created_at,updated_at,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (payload.collection_id, payload.track_id, payload.volume_id, payload.chapter_id, s['id'], s['checksum'], payload.piece_kind, title, version, 'ready', 'raw', 1, req_id, content, content, f"storage/generated/{req_id}.md", 'generated', payload.user_id, 'mock', 'mock-v1', json.dumps({'blocks': blocks}), json.dumps(payload.prompt_context_json), hashlib.sha256(content.encode()).hexdigest(), previous['id'] if previous else None, utc_now(), utc_now(), utc_now())).lastrowid
        if previous:
            conn.execute("UPDATE generated_pieces SET is_current=0,superseded_by_piece_id=?,updated_at=? WHERE id=?", (pid, utc_now(), previous['id']))
        conn.execute("INSERT OR REPLACE INTO piece_reuse_index (generation_key,generated_piece_id,is_active,created_at,updated_at) VALUES (?,?,?,?,?)", (gk, pid, 1, utc_now(), utc_now()))
        conn.execute("UPDATE generation_requests SET status='completed',execution_mode=?,provider_name=?,model_name=?,attempt_count=attempt_count+1,updated_at=?,completed_at=? WHERE id=?", (settings.generation_mode, 'mock', 'mock-v1', utc_now(), utc_now(), req_id))
        return {'request_id': req_id, 'status': 'completed', 'generated_piece': dict(conn.execute("SELECT * FROM generated_pieces WHERE id=?", (pid,)).fetchone())}
    except Exception as exc:
        conn.execute("UPDATE generation_requests SET status='failed',failure_reason=?,attempt_count=attempt_count+1,updated_at=? WHERE id=?", (str(exc), utc_now(), req_id))
        conn.commit()
        if settings.generation_mode == 'real_llm' and not settings.allow_mock_fallback:
            raise HTTPException(502, f"Falha real de geração: {exc}")
        raise HTTPException(500, str(exc))


@app.post('/api/v1/generation/requests')
def gen_request(payload: GenerationRequestIn):
    with get_conn() as conn:
        return _gen_request_with_conn(conn, payload)


@app.get('/api/v1/generation/requests/{request_id}')
def get_req(request_id: int):
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM generation_requests WHERE id=?", (request_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Request não encontrado")
        return dict(r)


# pieces/editorial
@app.get('/api/v1/generated-pieces/{piece_id}')
def piece(piece_id: int):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Peça não encontrada")
        return dict(p)


@app.get('/api/v1/chapters/{chapter_id}/generated-pieces/current')
def current_chapter(chapter_id: int, piece_kind: str = 'chapter'):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM generated_pieces WHERE chapter_id=? AND piece_kind=? AND is_current=1 AND status='ready' AND review_state!='rejected' AND status!='archived' ORDER BY id DESC LIMIT 1", (chapter_id, piece_kind)).fetchone()
        if not p:
            raise HTTPException(404, "Sem peça current")
        return dict(p)


@app.get('/api/v1/volumes/{volume_id}/generated-pieces/current')
def current_volume(volume_id: int, piece_kind: str = 'chapter'):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM generated_pieces WHERE volume_id=? AND chapter_id IS NULL AND piece_kind=? AND is_current=1 ORDER BY id DESC LIMIT 1", (volume_id, piece_kind)).fetchone()
        if not p:
            raise HTTPException(404, "Sem peça current")
        return dict(p)


@app.get('/api/v1/generated-pieces/{piece_id}/history')
def history(piece_id: int):
    with get_conn() as conn:
        base = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone()
        if not base:
            raise HTTPException(404, "Peça não encontrada")
        rows = conn.execute("SELECT id,version,review_state,is_current,created_at,supersedes_piece_id,superseded_by_piece_id,status FROM generated_pieces WHERE track_id=? AND IFNULL(volume_id,-1)=IFNULL(?,-1) AND IFNULL(chapter_id,-1)=IFNULL(?,-1) AND piece_kind=? ORDER BY version", (base['track_id'], base['volume_id'], base['chapter_id'], base['piece_kind'])).fetchall()
        return {'piece_id': piece_id, 'history': [dict(r) for r in rows]}


@app.get('/api/v1/generated-pieces/{piece_id}/diff')
def diff(piece_id: int, against_piece_id: int):
    with get_conn() as conn:
        a = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone()
        b = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (against_piece_id,)).fetchone()
        if not a or not b:
            raise HTTPException(404, "Peças para diff não encontradas")
        lines = list(difflib.unified_diff((a['content_markdown'] or '').splitlines(), (b['content_markdown'] or '').splitlines(), fromfile=f"piece_{a['id']}", tofile=f"piece_{b['id']}", lineterm=''))
        return {'summary': f'{len(lines)} linhas de diff', 'diff_text': '\n'.join(lines)}


@app.post('/api/v1/admin/generated-pieces/{piece_id}/review')
def review(piece_id: int, payload: ReviewIn):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone()
        if not p:
            raise HTTPException(404, 'Peça não encontrada')
        dt = payload.decision_type
        now = utc_now()
        if dt == 'mark_reviewed':
            conn.execute("UPDATE generated_pieces SET review_state='reviewed',reviewer_user_id=?,review_notes_text=?,updated_at=? WHERE id=?", (payload.decided_by_user_id, payload.decision_notes, now, piece_id))
        elif dt == 'approve':
            if not (p['content_markdown'] or '').strip():
                raise HTTPException(400, 'Peça vazia não pode ser aprovada')
            conn.execute("UPDATE generated_pieces SET review_state='approved',approved_at=?,reviewer_user_id=?,review_notes_text=?,updated_at=? WHERE id=?", (now, payload.decided_by_user_id, payload.decision_notes, now, piece_id))
        elif dt == 'reject':
            conn.execute("UPDATE generated_pieces SET review_state='rejected',is_current=0,rejected_at=?,updated_at=? WHERE id=?", (now, now, piece_id))
        elif dt == 'archive':
            conn.execute("UPDATE generated_pieces SET status='archived',is_current=0,archived_at=?,updated_at=? WHERE id=?", (now, now, piece_id))
        elif dt == 'promote_current':
            if p['review_state'] == 'rejected' or p['status'] == 'archived':
                raise HTTPException(400, 'Promoção inconsistente')
            set_single_current(conn, p)
        elif dt == 'request_revision':
            conn.execute("UPDATE generated_pieces SET review_notes_text=?,updated_at=? WHERE id=?", (payload.decision_notes, now, piece_id))
        else:
            raise HTTPException(400, 'decision_type inválido')
        log_review(conn, piece_id, payload.decided_by_user_id, dt, payload.decision_notes, payload.checklist)
        return {'status': 'ok', 'piece': dict(conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone())}


@app.post('/api/v1/admin/generated-pieces/{piece_id}/request-revision')
def request_revision(piece_id: int, payload: ReviewIn):
    payload.decision_type = 'request_revision'
    return review(piece_id, payload)


@app.post('/api/v1/admin/generated-pieces/{piece_id}/promote-current')
def promote(piece_id: int):
    return review(piece_id, ReviewIn(decided_by_user_id=0, decision_type='promote_current'))


@app.post('/api/v1/admin/generated-pieces/{piece_id}/archive')
def archive(piece_id: int):
    return review(piece_id, ReviewIn(decided_by_user_id=0, decision_type='archive'))


@app.get('/api/v1/admin/generation/requests/{request_id}/trace')
def trace(request_id: int):
    with get_conn() as conn:
        req = conn.execute("SELECT * FROM generation_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(404, 'Request não encontrado')
        p = conn.execute("SELECT generation_trace_json FROM generated_pieces WHERE generation_request_id=?", (request_id,)).fetchone()
        return {'request': dict(req), 'trace': json.loads(p[0]) if p and p[0] else {}}


@app.post('/api/v1/admin/generated-pieces/{piece_id}/review-state')
def review_state(piece_id: int, payload: ReviewStateIn):
    with get_conn() as conn:
        conn.execute("UPDATE generated_pieces SET review_state=?,updated_at=? WHERE id=?", (payload.review_state, utc_now(), piece_id))
        return {'status': 'ok'}


@app.get('/api/v1/admin/syllabi')
def admin_syllabi(status: str | None = None):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM syllabi ORDER BY id DESC").fetchall() if not status else conn.execute("SELECT * FROM syllabi WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        return [dict(r) for r in rows]


@app.post('/api/v1/admin/syllabi/import')
def import_syllabus(payload: ImportSyllabusIn):
    with get_conn() as conn:
        checksum = hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True).encode()).hexdigest()
        conn.execute("UPDATE syllabi SET status='archived',updated_at=? WHERE track_id=? AND volume_id=? AND IFNULL(chapter_id,-1)=IFNULL(?,-1)", (utc_now(), payload.track_id, payload.volume_id, payload.chapter_id))
        sid = conn.execute("INSERT INTO syllabi (collection_id,track_id,volume_id,chapter_id,version,status,title,objectives_json,program_content_json,notes_json,source_master_ref,checksum,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (payload.collection_id, payload.track_id, payload.volume_id, payload.chapter_id, payload.version, 'active', payload.title, json.dumps(payload.objectives_json), json.dumps(payload.program_content_json), json.dumps(payload.notes_json), payload.source_master_ref, checksum, utc_now(), utc_now())).lastrowid
        return {'id': sid, 'checksum': checksum}


# user
@app.post('/api/v1/progress/upsert')
def progress_upsert(payload: ProgressIn):
    settings = get_settings()
    with get_conn() as conn:
        now = utc_now()
        row = conn.execute("SELECT * FROM user_progress WHERE user_id=? AND collection_id=? AND track_id=? AND IFNULL(volume_id,-1)=IFNULL(?,-1) AND IFNULL(chapter_id,-1)=IFNULL(?,-1)", (payload.user_id, payload.collection_id, payload.track_id, payload.volume_id, payload.chapter_id)).fetchone()
        journey, rr_at, la_at = ("in_progress", None, None)
        if row:
            sample = dict(row)
            sample['state'] = payload.state
            sample['last_accessed_at'] = now
            journey, rr_at, la_at = compute_journey_state(sample, settings)
            anchor = json.dumps({'generated_piece_id': payload.generated_piece_id, 'chapter_id': payload.chapter_id, 'action': 'resume'})
            conn.execute("UPDATE user_progress SET generated_piece_id=?,progress_percent=?,state=?,journey_state=?,resume_anchor_json=?,last_accessed_at=?,last_meaningful_action_at=?,updated_at=?,completed_at=?,review_recommended_at=?,likely_abandoned_at=? WHERE id=?", (payload.generated_piece_id, payload.progress_percent, payload.state, journey, anchor, now, now if payload.progress_percent >= 5 else row['last_meaningful_action_at'], now, now if payload.state == 'completed' else None, rr_at, la_at, row['id']))
            pid = row['id']
        else:
            anchor = json.dumps({'generated_piece_id': payload.generated_piece_id, 'chapter_id': payload.chapter_id, 'action': 'resume'})
            conn.execute("INSERT INTO user_progress (user_id,generated_piece_id,collection_id,track_id,volume_id,chapter_id,progress_percent,state,journey_state,resume_anchor_json,started_at,last_accessed_at,completed_at,last_meaningful_action_at,review_recommended_at,likely_abandoned_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (payload.user_id, payload.generated_piece_id, payload.collection_id, payload.track_id, payload.volume_id, payload.chapter_id, payload.progress_percent, payload.state, 'in_progress', anchor, now, now, now if payload.state == 'completed' else None, now if payload.progress_percent >= 5 else None, rr_at, la_at, now, now))
            pid = conn.execute("SELECT last_insert_rowid() id").fetchone()['id']
        return {'id': pid, 'status': 'ok'}


@app.get('/api/v1/progress/me')
def progress_me(user_id: int, include_recommendation_flags: bool = False):
    with get_conn() as conn:
        recs = []
        if include_recommendation_flags:
            recs = compute_recommendations(conn, user_id, persist=True)
        rows = [dict(r) for r in conn.execute("SELECT * FROM user_progress WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()]
        if include_recommendation_flags:
            for r in rows:
                r['has_recommendation'] = any(x['target_piece_id'] == r.get('generated_piece_id') or x['target_chapter_id'] == r.get('chapter_id') for x in recs)
        return rows


@app.post('/api/v1/generated-pieces/{piece_id}/access-log')
def access_log(piece_id: int, payload: AccessLogIn):
    with get_conn() as conn:
        aid = conn.execute("INSERT INTO piece_access_log (user_id,generated_piece_id,access_type,created_at,metadata_json) VALUES (?,?,?,?,?)", (payload.user_id, piece_id, payload.access_type, utc_now(), json.dumps(payload.metadata_json))).lastrowid
        return {'id': aid, 'status': 'ok'}


@app.post('/api/v1/notes')
def note(payload: NoteIn):
    with get_conn() as conn:
        nid = conn.execute("INSERT INTO user_notes (user_id,generated_piece_id,volume_id,chapter_id,note_type,content,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (payload.user_id, payload.generated_piece_id, payload.volume_id, payload.chapter_id, payload.note_type, payload.content, utc_now(), utc_now())).lastrowid
        return {'id': nid, 'status': 'ok'}


@app.get('/api/v1/notes/me')
def notes_me(user_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM user_notes WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()]


# phase04 endpoints
@app.get('/api/v1/journey/me/recommendations')
def journey_recs(user_id: int):
    with get_conn() as conn:
        compute_recommendations(conn, user_id, persist=True)
        rows = conn.execute("SELECT * FROM user_recommendations WHERE user_id=? AND is_active=1 AND dismissed_at IS NULL ORDER BY priority_rank", (user_id,)).fetchall()
        return [dict(r) for r in rows]


@app.get('/api/v1/journey/me/next-step')
def journey_next_step(user_id: int):
    recs = journey_recs(user_id)
    if not recs:
        return {"recommendation_type": "none", "reason_text": "Sem recomendação ativa no momento."}
    return recs[0]


@app.get('/api/v1/journey/me/resume')
def journey_resume(user_id: int):
    with get_conn() as conn:
        compute_recommendations(conn, user_id, persist=True)
        p = conn.execute("SELECT * FROM user_progress WHERE user_id=? ORDER BY updated_at DESC LIMIT 1", (user_id,)).fetchone()
        rec = conn.execute("SELECT * FROM user_recommendations WHERE user_id=? AND recommendation_type='resume_piece' AND is_active=1 ORDER BY priority_rank LIMIT 1", (user_id,)).fetchone()
        return {"resume_anchor": json.loads(p['resume_anchor_json']) if p and p['resume_anchor_json'] else {}, "primary_recommendation": dict(rec) if rec else {}}


@app.post('/api/v1/journey/me/recommendations/{recommendation_id}/dismiss')
def dismiss_recommendation(recommendation_id: int, user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE user_recommendations SET is_active=0,dismissed_at=?,updated_at=? WHERE id=? AND user_id=?", (utc_now(), utc_now(), recommendation_id, user_id))
        return {'status': 'ok'}


# UI
def shell(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<html><head><title>{title}</title><style>
    body{{font-family:Inter,Arial;background:#0f172a;color:#e2e8f0;padding:24px}}
    .card{{background:#111827;border:1px solid #334155;padding:16px;border-radius:10px;margin-bottom:12px}}
    </style></head><body><h1>EIXO Formação / EIXO Trilhas</h1>{body}</body></html>""")


@app.get('/', response_class=HTMLResponse)
def ui_home(user_id: int = 1):
    with get_conn() as conn:
        recs = compute_recommendations(conn, user_id, persist=True)
        cards = "".join([f"<li>{r['recommendation_type']}: {r['reason_text']}</li>" for r in recs[:4]])
    return shell('Início', f"<div class='card'><h3>Continuar de onde parei</h3><ul>{cards}</ul></div>")


@app.get('/ui/collections', response_class=HTMLResponse)
def ui_collections():
    with get_conn() as conn:
        items = ''.join([f"<li>{r['code']} - {r['name']}</li>" for r in conn.execute("SELECT * FROM collections").fetchall()])
    return shell('Coleções', f"<div class='card'><ul>{items}</ul></div>")


@app.get('/ui/tracks/{collection_id}', response_class=HTMLResponse)
def ui_tracks(collection_id: int):
    with get_conn() as conn:
        items = ''.join([f"<li>{r['code']} - {r['name']}</li>" for r in conn.execute("SELECT * FROM tracks WHERE collection_id=?", (collection_id,)).fetchall()])
    return shell('Trilha', f"<div class='card'><ul>{items}</ul></div>")


@app.get('/ui/volume/{volume_id}', response_class=HTMLResponse)
def ui_volume(volume_id: int, user_id: int = 1):
    with get_conn() as conn:
        v = conn.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
        p = conn.execute("SELECT * FROM user_progress WHERE user_id=? AND volume_id=? ORDER BY updated_at DESC LIMIT 1", (user_id, volume_id)).fetchone()
    status = p['journey_state'] if p else 'not_started'
    return shell('Volume', f"<div class='card'>{v['code']} - {v['title']} | estado: {status}</div>")


@app.get('/ui/chapter/{chapter_id}', response_class=HTMLResponse)
def ui_chapter(chapter_id: int, user_id: int = 1):
    with get_conn() as conn:
        ch = conn.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        current = conn.execute("SELECT id FROM generated_pieces WHERE chapter_id=? AND is_current=1", (chapter_id,)).fetchone()
        p = conn.execute("SELECT * FROM user_progress WHERE user_id=? AND chapter_id=? ORDER BY updated_at DESC LIMIT 1", (user_id, chapter_id)).fetchone()
    state = p['journey_state'] if p else 'not_started'
    return shell('Capítulo', f"<div class='card'>{ch['code']} - {ch['title']} | peça_current={current['id'] if current else '-'} | estado={state}</div>")


@app.get('/ui/generation', response_class=HTMLResponse)
def ui_generation():
    with get_conn() as conn:
        req = conn.execute("SELECT id,status,execution_mode,failure_reason FROM generation_requests ORDER BY id DESC LIMIT 1").fetchone()
    text = 'Sem request' if not req else f"#{req['id']} {req['status']} {req['execution_mode']} {req['failure_reason'] or ''}"
    return shell('Solicitação', f"<div class='card'>{text}</div>")


@app.get('/ui/piece/{piece_id}', response_class=HTMLResponse)
def ui_piece(piece_id: int):
    with get_conn() as conn:
        p = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone()
    meta = f"v{p['version']} • {p['review_state']} • {'current' if p['is_current'] else 'histórica'}"
    return shell('Leitura', f"<div class='card'><small>{meta}</small><button>Retomar depois</button><pre>{p['content_markdown']}</pre></div>")


@app.get('/ui/journey/{user_id}', response_class=HTMLResponse)
def ui_journey(user_id: int):
    with get_conn() as conn:
        compute_recommendations(conn, user_id, persist=True)
        rows = conn.execute("SELECT * FROM user_progress WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
    items = ''.join([f"<li>{r['journey_state']} - capítulo {r['chapter_id']} ({r['progress_percent']}%)</li>" for r in rows])
    return shell('Minha Jornada', f"<div class='card'><ul>{items}</ul></div>")


@app.get('/ui/saved-pieces', response_class=HTMLResponse)
def ui_saved(review_state: str | None = None, only_current: bool = False):
    with get_conn() as conn:
        q = "SELECT * FROM generated_pieces WHERE 1=1"
        args: list[Any] = []
        if review_state:
            q += " AND review_state=?"
            args.append(review_state)
        if only_current:
            q += " AND is_current=1"
        rows = conn.execute(q + " ORDER BY id DESC", tuple(args)).fetchall()
    items = ''.join([f"<li>{r['id']} v{r['version']} {r['review_state']} current={r['is_current']} status={r['status']}</li>" for r in rows])
    return shell('Peças Salvas', f"<div class='card'><ul>{items}</ul></div>")


@app.get('/ui/notes/{user_id}', response_class=HTMLResponse)
def ui_notes(user_id: int):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM user_notes WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
    items = ''.join([f"<li>{r['note_type']}: {r['content']}</li>" for r in rows])
    return shell('Notas', f"<div class='card'><ul>{items}</ul></div>")


@app.get('/ui/admin', response_class=HTMLResponse)
def ui_admin():
    with get_conn() as conn:
        rows = conn.execute("SELECT id,version,review_state,is_current,origin,generator_provider,generator_model FROM generated_pieces ORDER BY id DESC LIMIT 10").fetchall()
    items = ''.join([f"<li>#{r['id']} v{r['version']} {r['review_state']} current={r['is_current']} {r['origin']} ({r['generator_provider']}/{r['generator_model']})</li>" for r in rows])
    return shell('Painel Administrativo', f"<div class='card'><ul>{items}</ul></div>")


class CurriculumImportBatchIn(BaseModel):
    source_type: str = "payload"
    source_name: str
    imported_by_user_id: int
    payload: dict[str, Any]


def normalize_curriculum_payload(payload: dict[str, Any]) -> dict[str, Any]:
    def clean(v):
        if isinstance(v, str):
            return " ".join(v.strip().split())
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, dict):
            return {k: clean(v[k]) for k in sorted(v.keys())}
        return v

    data = clean(payload)
    for key in ["tracks", "volumes", "chapters", "syllabi"]:
        if key in data and isinstance(data[key], list):
            data[key] = sorted(data[key], key=lambda x: (str(x.get("code", "")), int(x.get("canonical_order", 0))))
    return data


def payload_checksum(payload: dict[str, Any]) -> str:
    normalized = normalize_curriculum_payload(payload)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_curriculum(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    issues = []
    p = normalize_curriculum_payload(payload)

    required = ["collection", "tracks", "volumes", "chapters", "syllabi"]
    for r in required:
        if r not in p:
            issues.append({"issue_type": "missing_required_field", "severity": "error", "target_kind": "payload", "target_ref": r, "message": f"Campo obrigatório ausente: {r}", "details_json": {}})

    tracks = p.get("tracks", [])
    volumes = p.get("volumes", [])
    chapters = p.get("chapters", [])
    syllabi = p.get("syllabi", [])

    def dup(items, key, kind):
        seen = set()
        for i in items:
            v = i.get(key)
            if v in seen:
                issues.append({"issue_type": f"duplicate_{key}", "severity": "error", "target_kind": kind, "target_ref": str(v), "message": f"{key} duplicado", "details_json": i})
            else:
                seen.add(v)

    dup(tracks, "code", "track")
    dup(volumes, "code", "volume")
    dup(chapters, "code", "chapter")

    # duplicate order per parent
    by_track = {}
    for v in volumes:
        by_track.setdefault(v.get("track_code"), set())
        if v.get("canonical_order") in by_track[v.get("track_code")]:
            issues.append({"issue_type": "duplicate_order", "severity": "error", "target_kind": "volume", "target_ref": v.get("code"), "message": "canonical_order duplicado", "details_json": v})
        by_track[v.get("track_code")].add(v.get("canonical_order"))

    vol_codes = {v.get("code") for v in volumes}
    for c in chapters:
        if c.get("volume_code") not in vol_codes:
            issues.append({"issue_type": "orphan_chapter", "severity": "error", "target_kind": "chapter", "target_ref": c.get("code"), "message": "chapter sem volume válido", "details_json": c})

    allowed_status = {"active", "authorized", "archived"}
    for item, kind in [(tracks, "track"), (volumes, "volume"), (chapters, "chapter"), (syllabi, "syllabus")]:
        for it in item:
            if it.get("status") and it.get("status") not in allowed_status:
                issues.append({"issue_type": "invalid_status", "severity": "error", "target_kind": kind, "target_ref": it.get("code", "-"), "message": "status inválido", "details_json": it})

    for s in syllabi:
        if not s.get("program_content"):
            issues.append({"issue_type": "empty_program_content", "severity": "error", "target_kind": "syllabus", "target_ref": str(s.get("chapter_code")), "message": "conteúdo programático vazio", "details_json": s})

    report = {
        "errors": len([i for i in issues if i["severity"] == "error"]),
        "warnings": len([i for i in issues if i["severity"] == "warning"]),
        "checksum": payload_checksum(p),
    }
    return issues, {"normalized": p, "report": report}


@app.post('/api/v1/admin/curriculum/import-batches')
def create_import_batch(payload: CurriculumImportBatchIn):
    with get_conn() as conn:
        source_hash = hashlib.sha256(json.dumps(payload.payload, sort_keys=True).encode()).hexdigest()
        bid = conn.execute("INSERT INTO curriculum_import_batches (source_type,source_name,source_hash,imported_by_user_id,status,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (payload.source_type, payload.source_name, source_hash, payload.imported_by_user_id, "uploaded", json.dumps(payload.payload), utc_now(), utc_now())).lastrowid
        return {"id": bid, "status": "uploaded"}


@app.get('/api/v1/admin/curriculum/import-batches')
def list_import_batches():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM curriculum_import_batches ORDER BY id DESC").fetchall()]


@app.get('/api/v1/admin/curriculum/import-batches/{batch_id}')
def get_import_batch(batch_id: int):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM curriculum_import_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "batch não encontrado")
        return dict(b)


@app.post('/api/v1/admin/curriculum/import-batches/{batch_id}/validate')
def validate_import_batch(batch_id: int):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM curriculum_import_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "batch não encontrado")
        payload = json.loads(b["payload_json"])
        issues, result = validate_curriculum(payload)
        conn.execute("DELETE FROM curriculum_import_issues WHERE import_batch_id=?", (batch_id,))
        for i in issues:
            conn.execute("INSERT INTO curriculum_import_issues (import_batch_id,issue_type,severity,target_kind,target_ref,message,details_json,created_at) VALUES (?,?,?,?,?,?,?,?)", (batch_id, i["issue_type"], i["severity"], i["target_kind"], str(i["target_ref"]), i["message"], json.dumps(i["details_json"]), utc_now()))
        status = "preview_ready" if result["report"]["errors"] == 0 else "failed"
        conn.execute("UPDATE curriculum_import_batches SET status=?,preview_json=?,validation_report_json=?,updated_at=? WHERE id=?", (status, json.dumps(result["normalized"]), json.dumps(result["report"]), utc_now(), batch_id))
        return {"batch_id": batch_id, "status": status, "report": result["report"]}


@app.post('/api/v1/admin/curriculum/import-batches/{batch_id}/apply')
def apply_import_batch(batch_id: int, applied_by_user_id: int = 0):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM curriculum_import_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "batch não encontrado")
        if b["status"] != "preview_ready":
            raise HTTPException(400, "batch não validado para apply")

        preview = json.loads(b["preview_json"])
        checksum = payload_checksum(preview)

        existing = conn.execute("SELECT * FROM curriculum_versions WHERE checksum=?", (checksum,)).fetchone()
        if existing:
            conn.execute("UPDATE curriculum_import_batches SET status='applied',applied_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), batch_id))
            return {"status": "applied", "version_id": existing["id"], "idempotent": True}

        col = preview["collection"]
        conn.execute("INSERT OR REPLACE INTO collections (id,code,name,description,is_active,created_at,updated_at) VALUES ((SELECT id FROM collections WHERE code=?),?,?,?,?,?,?)", (col["code"], col["code"], col.get("name", col["code"]), col.get("description", ""), 1, utc_now(), utc_now()))
        collection_id = conn.execute("SELECT id FROM collections WHERE code=?", (col["code"],)).fetchone()["id"]

        track_ids = {}
        for t in preview.get("tracks", []):
            existing = conn.execute("SELECT id FROM tracks WHERE code=? AND collection_id=?", (t["code"], collection_id)).fetchone()
            if existing:
                conn.execute("UPDATE tracks SET name=?,description=?,canonical_order=?,status=?,is_active=1,updated_at=? WHERE id=?", (t.get("name", t["code"]), t.get("description", ""), t.get("canonical_order", 1), t.get("status", "active"), utc_now(), existing["id"]))
                tid = existing["id"]
            else:
                tid = conn.execute("INSERT INTO tracks (collection_id,code,name,description,canonical_order,status,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (collection_id, t["code"], t.get("name", t["code"]), t.get("description", ""), t.get("canonical_order", 1), t.get("status", "active"), 1, utc_now(), utc_now())).lastrowid
            track_ids[t["code"]] = tid

        volume_ids = {}
        for v in preview.get("volumes", []):
            tid = track_ids[v["track_code"]]
            existing = conn.execute("SELECT id FROM volumes WHERE code=? AND track_id=?", (v["code"], tid)).fetchone()
            if existing:
                conn.execute("UPDATE volumes SET title=?,summary=?,canonical_order=?,status=?,source_master_ref=?,updated_at=? WHERE id=?", (v.get("title", v["code"]), v.get("summary", ""), v.get("canonical_order", 1), v.get("status", "authorized"), v.get("source_master_ref", "import"), utc_now(), existing["id"]))
                vid = existing["id"]
            else:
                vid = conn.execute("INSERT INTO volumes (collection_id,track_id,code,title,summary,canonical_order,status,source_master_ref,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (collection_id, tid, v["code"], v.get("title", v["code"]), v.get("summary", ""), v.get("canonical_order", 1), v.get("status", "authorized"), v.get("source_master_ref", "import"), utc_now(), utc_now())).lastrowid
            volume_ids[v["code"]] = vid

        chapter_ids = {}
        for c in preview.get("chapters", []):
            vid = volume_ids[c["volume_code"]]
            existing = conn.execute("SELECT id FROM chapters WHERE code=? AND volume_id=?", (c["code"], vid)).fetchone()
            if existing:
                conn.execute("UPDATE chapters SET title=?,summary=?,canonical_order=?,status=?,updated_at=? WHERE id=?", (c.get("title", c["code"]), c.get("summary", ""), c.get("canonical_order", 1), c.get("status", "authorized"), utc_now(), existing["id"]))
                cid = existing["id"]
            else:
                cid = conn.execute("INSERT INTO chapters (volume_id,code,title,summary,canonical_order,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (vid, c["code"], c.get("title", c["code"]), c.get("summary", ""), c.get("canonical_order", 1), c.get("status", "authorized"), utc_now(), utc_now())).lastrowid
            chapter_ids[c["code"]] = cid

        # archive old syllabi and insert new active
        conn.execute("UPDATE syllabi SET status='archived',updated_at=?", (utc_now(),))
        for s in preview.get("syllabi", []):
            ch_id = chapter_ids[s["chapter_code"]]
            vol_id = conn.execute("SELECT volume_id FROM chapters WHERE id=?", (ch_id,)).fetchone()["volume_id"]
            track_id = conn.execute("SELECT track_id FROM volumes WHERE id=?", (vol_id,)).fetchone()["track_id"]
            sc = hashlib.sha256(json.dumps(normalize_curriculum_payload(s), sort_keys=True).encode()).hexdigest()
            conn.execute("INSERT INTO syllabi (collection_id,track_id,volume_id,chapter_id,version,status,title,objectives_json,program_content_json,notes_json,source_master_ref,checksum,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (collection_id, track_id, vol_id, ch_id, s.get("version", "1.0.0"), s.get("status", "active"), s.get("title", f"Ementa {s['chapter_code']}"), json.dumps(s.get("objectives", [])), json.dumps(s.get("program_content", [])), json.dumps(s.get("notes", [])), s.get("source_master_ref", "import"), sc, utc_now(), utc_now()))

        version_id = conn.execute("INSERT INTO curriculum_versions (import_batch_id,collection_id,version_label,checksum,applied_by_user_id,created_at) VALUES (?,?,?,?,?,?)", (batch_id, collection_id, preview.get("version_label", f"batch-{batch_id}"), checksum, applied_by_user_id, utc_now())).lastrowid
        conn.execute("UPDATE curriculum_import_batches SET status='applied',applied_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), batch_id))
        return {"status": "applied", "version_id": version_id, "idempotent": False}


@app.get('/api/v1/admin/curriculum/import-batches/{batch_id}/issues')
def get_import_issues(batch_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM curriculum_import_issues WHERE import_batch_id=? ORDER BY id", (batch_id,)).fetchall()]


@app.get('/api/v1/admin/curriculum/versions')
def curriculum_versions_list():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM curriculum_versions ORDER BY id DESC").fetchall()]


@app.get('/api/v1/admin/curriculum/versions/{version_id}')
def curriculum_version_detail(version_id: int):
    with get_conn() as conn:
        v = conn.execute("SELECT * FROM curriculum_versions WHERE id=?", (version_id,)).fetchone()
        if not v:
            raise HTTPException(404, "versão não encontrada")
        return dict(v)


@app.get('/api/v1/admin/curriculum/preview-diff')
def curriculum_preview_diff(batch_id: int, against_version_id: int):
    with get_conn() as conn:
        b = conn.execute("SELECT * FROM curriculum_import_batches WHERE id=?", (batch_id,)).fetchone()
        v = conn.execute("SELECT * FROM curriculum_versions WHERE id=?", (against_version_id,)).fetchone()
        if not b or not v:
            raise HTTPException(404, "batch/version não encontrado")
        preview = b["preview_json"] or b["payload_json"]
        diff = list(difflib.unified_diff((preview or "").splitlines(), [v["checksum"]], fromfile='preview', tofile='version_checksum', lineterm=''))
        return {"summary": f"{len(diff)} linhas", "diff_text": "\n".join(diff)}

class EditorialJobIn(BaseModel):
    job_kind: str
    target_kind: str
    target_ref: str
    created_by_user_id: int | None = None
    priority: str = "normal"
    payload_json: dict[str, Any] = {}
    max_retries: int = 2


class EditorialBatchActionIn(BaseModel):
    action_kind: str
    created_by_user_id: int
    filters_json: dict[str, Any] = {}
    target_ids_json: list[int] = []


def _log_job(conn: sqlite3.Connection, job_id: int, event: str, message: str, details: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO job_execution_logs (editorial_job_id,event_type,message,details_json,created_at) VALUES (?,?,?,?,?)", (job_id, event, message, json.dumps(details or {}), utc_now()))


def _dedupe_key(job_kind: str, target_kind: str, target_ref: str, payload_json: dict[str, Any]) -> str:
    if job_kind == 'generate_piece' and payload_json.get('generation_key'):
        return f"generate_piece::{payload_json['generation_key']}"
    if job_kind == 'review_piece':
        return f"review_piece::{target_ref}"
    if job_kind == 'curriculum_apply':
        return f"curriculum_apply::{target_ref}"
    return f"{job_kind}::{target_kind}::{target_ref}"


def _execute_job(conn: sqlite3.Connection, job: sqlite3.Row) -> tuple[str, dict[str, Any], str | None]:
    kind = job['job_kind']
    payload = json.loads(job['payload_json'] or '{}')
    try:
        if kind == 'generate_piece':
            res = _gen_request_with_conn(conn, GenerationRequestIn(**payload))
            return 'completed', {'generation': res}, None
        if kind == 'review_piece':
            piece_id = int(job['target_ref'])
            piece = conn.execute("SELECT * FROM generated_pieces WHERE id=?", (piece_id,)).fetchone()
            if not piece:
                return 'failed', {}, 'piece inexistente'
            if piece['review_state'] not in ['raw', 'reviewed']:
                return 'blocked', {}, 'estado editorial não elegível para review queue'
            r = review(piece_id, ReviewIn(decided_by_user_id=payload.get('decided_by_user_id', 0), decision_type=payload.get('decision_type', 'mark_reviewed'), decision_notes=payload.get('decision_notes', 'queue review')))
            return 'completed', {'review': r}, None
        if kind == 'promote_current':
            r = promote(int(job['target_ref']))
            return 'completed', {'promote': r}, None
        if kind == 'archive_piece':
            r = archive(int(job['target_ref']))
            return 'completed', {'archive': r}, None
        if kind == 'recompute_recommendations':
            recs = journey_recs(int(job['target_ref']))
            return 'completed', {'recommendations': recs}, None
        if kind == 'curriculum_apply':
            r = apply_import_batch(int(job['target_ref']), payload.get('applied_by_user_id', 0))
            return 'completed', {'apply': r}, None
        return 'failed', {}, 'job_kind não suportado'
    except Exception as exc:
        return 'failed', {}, str(exc)


def _create_job_with_conn(conn: sqlite3.Connection, payload: EditorialJobIn):
    if payload.job_kind not in ['generate_piece','review_piece','request_revision','reprocess_piece','promote_current','archive_piece','curriculum_apply','recompute_recommendations']:
        raise HTTPException(400, 'job_kind inválido')
    if payload.priority not in ['low','normal','high','urgent']:
        raise HTTPException(400, 'priority inválida')
    dedupe = _dedupe_key(payload.job_kind, payload.target_kind, payload.target_ref, payload.payload_json)
    existing = conn.execute("SELECT * FROM editorial_jobs WHERE dedupe_key=? AND status IN ('queued','claimed','processing')", (dedupe,)).fetchone()
    if existing:
        _log_job(conn, existing['id'], 'deduplicated', 'job equivalente já ativo', {'dedupe_key': dedupe})
        return {'id': existing['id'], 'status': existing['status'], 'deduplicated': True}
    jid = conn.execute("INSERT INTO editorial_jobs (job_kind,target_kind,target_ref,created_by_user_id,priority,status,dedupe_key,payload_json,retry_count,max_retries,available_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (payload.job_kind, payload.target_kind, payload.target_ref, payload.created_by_user_id, payload.priority, 'queued', dedupe, json.dumps(payload.payload_json), 0, payload.max_retries, utc_now(), utc_now(), utc_now())).lastrowid
    _log_job(conn, jid, 'queued', 'job enfileirado', {'priority': payload.priority})
    return {'id': jid, 'status': 'queued', 'deduplicated': False}


@app.post('/api/v1/admin/jobs')
def create_job(payload: EditorialJobIn):
    with get_conn() as conn:
        return _create_job_with_conn(conn, payload)


@app.get('/api/v1/admin/jobs')
def list_jobs(job_kind: str | None = None, status: str | None = None, priority: str | None = None, target_kind: str | None = None, assigned_to_user_id: int | None = None, created_by_user_id: int | None = None):
    with get_conn() as conn:
        q = "SELECT * FROM editorial_jobs WHERE 1=1"
        args=[]
        for field,val in [('job_kind',job_kind),('status',status),('priority',priority),('target_kind',target_kind),('assigned_to_user_id',assigned_to_user_id),('created_by_user_id',created_by_user_id)]:
            if val is not None:
                q += f" AND {field}=?"; args.append(val)
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at", tuple(args)).fetchall()]


@app.get('/api/v1/admin/jobs/{job_id}')
def get_job(job_id: int):
    with get_conn() as conn:
        j = conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone()
        if not j:
            raise HTTPException(404, 'job não encontrado')
        return dict(j)


@app.post('/api/v1/admin/jobs/{job_id}/claim')
def claim_job(job_id: int, assigned_to_user_id: int = 0, process: bool = True):
    with get_conn() as conn:
        j = conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone()
        if not j:
            raise HTTPException(404, 'job não encontrado')
        if j['status'] in ['completed','cancelled']:
            raise HTTPException(400, 'job não elegível para claim')
        if j['locked_at'] and j['status'] in ['claimed','processing']:
            raise HTTPException(400, 'job já locked')
        conn.execute("UPDATE editorial_jobs SET status='claimed',assigned_to_user_id=?,locked_at=?,updated_at=? WHERE id=?", (assigned_to_user_id, utc_now(), utc_now(), job_id))
        _log_job(conn, job_id, 'claimed', 'job claimado', {'assigned_to': assigned_to_user_id})
        if process:
            j = conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone()
            conn.execute("UPDATE editorial_jobs SET status='processing',updated_at=? WHERE id=?", (utc_now(), job_id))
            _log_job(conn, job_id, 'started', 'execução iniciada', {})
            status, result, failure = _execute_job(conn, j)
            if status == 'completed':
                conn.execute("UPDATE editorial_jobs SET status='completed',result_json=?,completed_at=?,updated_at=? WHERE id=?", (json.dumps(result), utc_now(), utc_now(), job_id))
                _log_job(conn, job_id, 'completed', 'execução concluída', result)
            elif status == 'blocked':
                conn.execute("UPDATE editorial_jobs SET status='blocked',failure_reason=?,updated_at=? WHERE id=?", (failure, utc_now(), job_id))
                _log_job(conn, job_id, 'failed', 'job bloqueado', {'reason': failure})
            else:
                conn.execute("UPDATE editorial_jobs SET status='failed',failure_reason=?,retry_count=retry_count+1,updated_at=? WHERE id=?", (failure, utc_now(), job_id))
                _log_job(conn, job_id, 'failed', 'execução falhou', {'reason': failure})
        return dict(conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone())


@app.post('/api/v1/admin/jobs/{job_id}/cancel')
def cancel_job(job_id: int):
    with get_conn() as conn:
        j = conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone()
        if not j:
            raise HTTPException(404, 'job não encontrado')
        if j['status'] != 'queued':
            raise HTTPException(400, 'cancel permitido apenas para jobs em fila')
        conn.execute("UPDATE editorial_jobs SET status='cancelled',cancelled_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), job_id))
        _log_job(conn, job_id, 'cancelled', 'job cancelado', {})
        return {'status':'cancelled'}


@app.post('/api/v1/admin/jobs/{job_id}/retry')
def retry_job(job_id: int):
    with get_conn() as conn:
        j = conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone()
        if not j:
            raise HTTPException(404, 'job não encontrado')
        if j['status'] == 'completed':
            raise HTTPException(400, 'retry inválido para completed')
        if j['retry_count'] >= j['max_retries']:
            raise HTTPException(400, 'max retries atingido')
        conn.execute("UPDATE editorial_jobs SET status='queued',locked_at=NULL,failure_reason=NULL,updated_at=? WHERE id=?", (utc_now(), job_id))
        _log_job(conn, job_id, 'retried', 'job reenfileirado', {'retry_count': j['retry_count'] + 1})
        return dict(conn.execute("SELECT * FROM editorial_jobs WHERE id=?", (job_id,)).fetchone())


@app.get('/api/v1/admin/jobs/{job_id}/logs')
def job_logs(job_id: int):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM job_execution_logs WHERE editorial_job_id=? ORDER BY id", (job_id,)).fetchall()]


@app.post('/api/v1/admin/batch-actions')
def create_batch_action(payload: EditorialBatchActionIn):
    with get_conn() as conn:
        if not payload.filters_json and not payload.target_ids_json:
            raise HTTPException(400, 'batch action sem alvo material')
        bid = conn.execute("INSERT INTO editorial_batch_actions (action_kind,created_by_user_id,status,filters_json,target_ids_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (payload.action_kind, payload.created_by_user_id, 'processing', json.dumps(payload.filters_json), json.dumps(payload.target_ids_json), utc_now(), utc_now())).lastrowid
        created = 0
        if payload.action_kind == 'queue_generate_for_volume':
            volume_id = payload.filters_json.get('volume_id')
            chapters = conn.execute("SELECT * FROM chapters WHERE volume_id=? AND status='authorized'", (volume_id,)).fetchall()
            for c in chapters:
                vol = conn.execute("SELECT * FROM volumes WHERE id=?", (volume_id,)).fetchone()
                tr = conn.execute("SELECT * FROM tracks WHERE id=?", (vol['track_id'],)).fetchone()
                col = conn.execute("SELECT * FROM collections WHERE id=?", (vol['collection_id'],)).fetchone()
                res = _create_job_with_conn(conn, EditorialJobIn(job_kind='generate_piece', target_kind='chapter', target_ref=str(c['id']), created_by_user_id=payload.created_by_user_id, payload_json={'user_id': payload.created_by_user_id or 0, 'collection_id': col['id'], 'track_id': tr['id'], 'volume_id': vol['id'], 'chapter_id': c['id'], 'piece_kind': 'chapter', 'request_mode': 'create_if_missing', 'prompt_context_json': {}}))
                if not res.get('deduplicated'):
                    created += 1
        elif payload.action_kind == 'queue_review_for_track':
            track_id = payload.filters_json.get('track_id')
            pieces = conn.execute("SELECT * FROM generated_pieces WHERE track_id=? AND review_state='raw' AND status='ready'", (track_id,)).fetchall()
            for p in pieces:
                res = _create_job_with_conn(conn, EditorialJobIn(job_kind='review_piece', target_kind='piece', target_ref=str(p['id']), created_by_user_id=payload.created_by_user_id, payload_json={'decision_type': 'mark_reviewed', 'decided_by_user_id': payload.created_by_user_id or 0}))
                if not res.get('deduplicated'):
                    created += 1
        elif payload.action_kind == 'retry_failed_jobs':
            jobs = conn.execute("SELECT * FROM editorial_jobs WHERE status='failed'").fetchall()
            for j in jobs:
                try:
                    retry_job(j['id']); created += 1
                except Exception:
                    pass
        else:
            raise HTTPException(400, 'action_kind não suportado')
        conn.execute("UPDATE editorial_batch_actions SET status='completed',result_summary_json=?,completed_at=?,updated_at=? WHERE id=?", (json.dumps({'jobs_created_or_updated': created}), utc_now(), utc_now(), bid))
        return {'id': bid, 'status': 'completed', 'jobs_created_or_updated': created}


@app.get('/api/v1/admin/batch-actions')
def list_batch_actions():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM editorial_batch_actions ORDER BY id DESC").fetchall()]


@app.get('/api/v1/admin/backlog/summary')
def backlog_summary():
    with get_conn() as conn:
        def cnt(where):
            return conn.execute(f"SELECT COUNT(*) c FROM editorial_jobs WHERE {where}").fetchone()['c']
        return {
            'queued_count': cnt("status='queued'"),
            'processing_count': cnt("status IN ('claimed','processing')"),
            'failed_count': cnt("status='failed'"),
            'blocked_count': cnt("status='blocked'"),
            'review_pending_count': conn.execute("SELECT COUNT(*) c FROM generated_pieces WHERE review_state='raw' AND status='ready'").fetchone()['c'],
            'urgent_count': cnt("priority='urgent' AND status IN ('queued','claimed','processing')"),
            'completed_last_24h': conn.execute("SELECT COUNT(*) c FROM editorial_jobs WHERE status='completed' AND completed_at>=?", ((datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(),)).fetchone()['c'],
        }


@app.get('/api/v1/admin/backlog/review')
def backlog_review(track_id: int | None = None):
    with get_conn() as conn:
        q = "SELECT * FROM generated_pieces WHERE review_state='raw' AND status='ready'"
        args=[]
        if track_id is not None:
            q += " AND track_id=?"; args.append(track_id)
        q += " ORDER BY created_at"
        return [dict(r) for r in conn.execute(q, tuple(args)).fetchall()]
