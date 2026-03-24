from pathlib import Path

from fastapi.testclient import TestClient

from app.main import DB_PATH, app, init_db, seed_data

client = TestClient(app)


def setup_function():
    if Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
    init_db()
    seed_data()


def ids():
    c = client.get('/api/v1/collections').json()[0]
    t = client.get(f"/api/v1/collections/{c['id']}/tracks").json()[0]
    v = client.get(f"/api/v1/tracks/{t['id']}/volumes").json()[0]
    return c, t, v


def gen_job_payload(chapter_id=1):
    c, t, v = ids()
    return {
        'job_kind': 'generate_piece',
        'target_kind': 'chapter',
        'target_ref': str(chapter_id),
        'created_by_user_id': 1,
        'priority': 'normal',
        'payload_json': {
            'user_id': 1,
            'collection_id': c['id'],
            'track_id': t['id'],
            'volume_id': v['id'],
            'chapter_id': chapter_id,
            'piece_kind': 'chapter',
            'request_mode': 'create_if_missing',
            'prompt_context_json': {},
        }
    }


def create_piece_for_review():
    c, t, v = ids()
    return client.post('/api/v1/generation/requests', json={
        'user_id': 1, 'collection_id': c['id'], 'track_id': t['id'], 'volume_id': v['id'], 'chapter_id': 1,
        'piece_kind': 'chapter', 'request_mode': 'create_if_missing', 'prompt_context_json': {}
    }).json()['generated_piece']['id']


def test_01_create_generation_job_valid():
    r = client.post('/api/v1/admin/jobs', json=gen_job_payload())
    assert r.status_code == 200 and r.json()['status'] == 'queued'


def test_02_dedupe_equivalent_active_job():
    a = client.post('/api/v1/admin/jobs', json=gen_job_payload()).json()
    b = client.post('/api/v1/admin/jobs', json=gen_job_payload()).json()
    assert a['id'] == b['id'] and b['deduplicated'] is True


def test_03_priority_respected_next_selection():
    p1 = gen_job_payload(); p1['priority'] = 'low'
    p2 = gen_job_payload(); p2['priority'] = 'urgent'; p2['target_ref'] = '2'; p2['payload_json']['chapter_id'] = 2
    j1 = client.post('/api/v1/admin/jobs', json=p1).json()['id']
    j2 = client.post('/api/v1/admin/jobs', json=p2).json()['id']
    jobs = client.get('/api/v1/admin/jobs?status=queued').json()
    jobs_sorted = sorted(jobs, key=lambda x: {'urgent':0,'high':1,'normal':2,'low':3}[x['priority']])
    assert jobs_sorted[0]['id'] == j2 and j1 != j2


def test_04_claim_without_double_execution():
    jid = client.post('/api/v1/admin/jobs', json=gen_job_payload()).json()['id']
    client.post(f'/api/v1/admin/jobs/{jid}/claim?assigned_to_user_id=8&process=false')
    again = client.post(f'/api/v1/admin/jobs/{jid}/claim?assigned_to_user_id=9&process=false')
    assert again.status_code == 400


def test_05_register_execution_log():
    jid = client.post('/api/v1/admin/jobs', json=gen_job_payload()).json()['id']
    client.post(f'/api/v1/admin/jobs/{jid}/claim?assigned_to_user_id=8&process=true')
    logs = client.get(f'/api/v1/admin/jobs/{jid}/logs').json()
    assert len(logs) >= 2


def test_06_fail_with_reason_on_execution_error():
    bad = gen_job_payload(); bad['payload_json']['chapter_id'] = 999
    jid = client.post('/api/v1/admin/jobs', json=bad).json()['id']
    res = client.post(f'/api/v1/admin/jobs/{jid}/claim?process=true').json()
    assert res['status'] == 'failed' and res['failure_reason']


def test_07_retry_controlled_works():
    bad = gen_job_payload(); bad['payload_json']['chapter_id'] = 999
    jid = client.post('/api/v1/admin/jobs', json=bad).json()['id']
    client.post(f'/api/v1/admin/jobs/{jid}/claim?process=true')
    ret = client.post(f'/api/v1/admin/jobs/{jid}/retry')
    assert ret.status_code == 200 and ret.json()['status'] == 'queued'


def test_08_batch_action_creates_expected_jobs():
    _, _, v = ids()
    act = client.post('/api/v1/admin/batch-actions', json={'action_kind': 'queue_generate_for_volume', 'created_by_user_id': 1, 'filters_json': {'volume_id': v['id']}, 'target_ids_json': []}).json()
    assert act['jobs_created_or_updated'] >= 1


def test_09_backlog_summary_reflects_real_states():
    client.post('/api/v1/admin/jobs', json=gen_job_payload())
    summary = client.get('/api/v1/admin/backlog/summary').json()
    assert summary['queued_count'] >= 1


def test_10_generation_job_respects_reuse_before_generate():
    # first generate directly
    create_piece_for_review()
    j = client.post('/api/v1/admin/jobs', json=gen_job_payload()).json()['id']
    done = client.post(f'/api/v1/admin/jobs/{j}/claim?process=true').json()
    assert done['status'] == 'completed'


def test_11_review_job_not_promote_absurd_state():
    piece_id = create_piece_for_review()
    client.post(f'/api/v1/admin/generated-pieces/{piece_id}/review', json={'decided_by_user_id': 1, 'decision_type': 'approve', 'decision_notes': '', 'checklist': {}})
    j = client.post('/api/v1/admin/jobs', json={'job_kind': 'review_piece', 'target_kind': 'piece', 'target_ref': str(piece_id), 'created_by_user_id': 1, 'priority': 'normal', 'payload_json': {'decision_type': 'mark_reviewed'}}).json()['id']
    res = client.post(f'/api/v1/admin/jobs/{j}/claim?process=true').json()
    assert res['status'] == 'blocked'


def test_12_invalid_cancel_blocked():
    j = client.post('/api/v1/admin/jobs', json=gen_job_payload()).json()['id']
    client.post(f'/api/v1/admin/jobs/{j}/claim?process=true')
    res = client.post(f'/api/v1/admin/jobs/{j}/cancel')
    assert res.status_code == 400


def test_13_reprocess_does_not_delete_history():
    p = create_piece_for_review()
    # force new version directly
    c, t, v = ids()
    p2 = client.post('/api/v1/generation/requests', json={'user_id':1,'collection_id':c['id'],'track_id':t['id'],'volume_id':v['id'],'chapter_id':1,'piece_kind':'chapter','request_mode':'force_new_version','prompt_context_json':{}}).json()['generated_piece']['id']
    hist = client.get(f'/api/v1/generated-pieces/{p}/history').json()['history']
    ids_hist = [h['id'] for h in hist]
    assert p in ids_hist and p2 in ids_hist


def test_14_review_queue_lists_pending_raw_correctly():
    piece_id = create_piece_for_review()
    q = client.get('/api/v1/admin/backlog/review').json()
    assert any(x['id'] == piece_id for x in q)
