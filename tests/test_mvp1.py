import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import DB_PATH, app, init_db, seed_data

client = TestClient(app)


def setup_function():
    if Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
    init_db()
    seed_data()
    os.environ['GENERATION_MODE'] = 'mock'
    os.environ['JOURNEY_PAUSED_AFTER_HOURS'] = '1'
    os.environ['JOURNEY_ABANDONED_AFTER_DAYS'] = '2'
    os.environ['REVIEW_SUGGEST_AFTER_DAYS'] = '1'


def ids():
    c = client.get('/api/v1/collections').json()[0]
    t = client.get(f"/api/v1/collections/{c['id']}/tracks").json()[0]
    v = client.get(f"/api/v1/tracks/{t['id']}/volumes").json()[0]
    ch1 = client.get('/api/v1/chapters/1').json()
    ch2 = client.get('/api/v1/chapters/2').json()
    return c, t, v, ch1, ch2


def gen_payload(chapter_id=1, mode='create_if_missing'):
    c, t, v, _, _ = ids()
    return {'user_id': 1, 'collection_id': c['id'], 'track_id': t['id'], 'volume_id': v['id'], 'chapter_id': chapter_id, 'piece_kind': 'chapter', 'request_mode': mode, 'prompt_context_json': {}}


def create_piece(chapter_id=1):
    return client.post('/api/v1/generation/requests', json=gen_payload(chapter_id)).json()['generated_piece']['id']


def upsert_progress(piece_id, chapter_id=1, state='in_progress', pct=20):
    c, t, v, _, _ = ids()
    return client.post('/api/v1/progress/upsert', json={'user_id': 1, 'generated_piece_id': piece_id, 'collection_id': c['id'], 'track_id': t['id'], 'volume_id': v['id'], 'chapter_id': chapter_id, 'progress_percent': pct, 'state': state})


def test_01_suggest_resume_in_progress():
    p = create_piece()
    upsert_progress(p, pct=30)
    rec = client.get('/api/v1/journey/me/next-step?user_id=1').json()
    assert rec['recommendation_type'] == 'resume_piece'


def test_02_mark_paused_after_window():
    p = create_piece()
    upsert_progress(p, pct=20)
    conn = sqlite3.connect(DB_PATH)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    conn.execute("UPDATE user_progress SET last_accessed_at=? WHERE user_id=1", (old,))
    conn.commit(); conn.close()
    rows = client.get('/api/v1/progress/me?user_id=1&include_recommendation_flags=true').json()
    assert rows[0]['journey_state'] == 'paused'


def test_03_mark_likely_abandoned_after_bigger_window():
    p = create_piece()
    upsert_progress(p, pct=20)
    conn = sqlite3.connect(DB_PATH)
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    conn.execute("UPDATE user_progress SET last_accessed_at=? WHERE user_id=1", (old,))
    conn.commit(); conn.close()
    rows = client.get('/api/v1/progress/me?user_id=1&include_recommendation_flags=true').json()
    assert rows[0]['journey_state'] == 'likely_abandoned'


def test_04_suggest_review_with_signal():
    p = create_piece()
    upsert_progress(p, state='completed', pct=100)
    client.post('/api/v1/notes', json={'user_id': 1, 'generated_piece_id': p, 'volume_id': 1, 'chapter_id': 1, 'note_type': 'duvida', 'content': 'nao entendi'})
    conn = sqlite3.connect(DB_PATH)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE user_progress SET last_accessed_at=? WHERE user_id=1", (old,))
    conn.commit(); conn.close()
    recs = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    assert any(r['recommendation_type'] == 'review_piece' for r in recs)


def test_05_suggest_next_chapter_when_previous_completed():
    p = create_piece(1)
    upsert_progress(p, chapter_id=1, state='completed', pct=100)
    p2 = create_piece(2)
    recs = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    assert any(r['recommendation_type'] == 'continue_track' and r['target_piece_id'] == p2 for r in recs)


def test_06_not_suggest_next_without_real_progress():
    p = create_piece(1)
    upsert_progress(p, chapter_id=1, state='in_progress', pct=1)
    recs = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    assert not any(r['recommendation_type'] == 'continue_track' for r in recs)


def test_07_not_suggest_rejected_archived_piece():
    p = create_piece()
    client.post(f'/api/v1/admin/generated-pieces/{p}/review', json={'decided_by_user_id': 9, 'decision_type': 'reject', 'decision_notes': 'ruim', 'checklist': {}})
    upsert_progress(p)
    recs = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    assert not any(r.get('target_piece_id') == p for r in recs)


def test_08_recommendation_has_reason_text():
    p = create_piece()
    upsert_progress(p)
    rec = client.get('/api/v1/journey/me/next-step?user_id=1').json()
    assert rec['reason_text']


def test_09_resume_respects_current_piece():
    p1 = create_piece(1)
    p2 = client.post('/api/v1/generation/requests', json=gen_payload(1, 'force_new_version')).json()['generated_piece']['id']
    upsert_progress(p2)
    resume = client.get('/api/v1/journey/me/resume?user_id=1').json()
    assert resume['resume_anchor']['generated_piece_id'] == p2
    assert resume['resume_anchor']['generated_piece_id'] != p1


def test_10_suggest_generate_missing_piece_for_authorized_target():
    p = create_piece(1)
    upsert_progress(p, chapter_id=1, state='completed', pct=100)
    # remove current piece for chapter 2
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM generated_pieces WHERE chapter_id=2")
    conn.commit(); conn.close()
    recs = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    assert any(r['recommendation_type'] == 'generate_missing_piece' and r['target_chapter_id'] == 2 for r in recs)


def test_11_dismiss_recommendation_works():
    p = create_piece()
    upsert_progress(p)
    recs = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    rid = recs[0]['id']
    client.post(f'/api/v1/journey/me/recommendations/{rid}/dismiss?user_id=1')
    recs2 = client.get('/api/v1/journey/me/recommendations?user_id=1').json()
    assert all(r['id'] != rid for r in recs2)


def test_12_journey_page_state_coherent():
    p = create_piece()
    upsert_progress(p, pct=40)
    html = client.get('/ui/journey/1').text
    assert 'in_progress' in html or 'paused' in html or 'likely_abandoned' in html
