import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import DB_PATH, app, init_db, seed_data

client = TestClient(app)


def setup_function():
    if Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
    init_db()
    seed_data()


def valid_payload():
    return {
        "collection": {"code": "LAB_A_ROCHA", "name": "Coleção Rocha", "description": "ok"},
        "tracks": [{"code": "EMBARCADOS", "name": "Trilha Embarcados", "canonical_order": 1, "status": "active"}],
        "volumes": [{"code": "V1", "track_code": "EMBARCADOS", "title": "Volume 1", "canonical_order": 1, "status": "authorized"}],
        "chapters": [
            {"code": "C1", "volume_code": "V1", "title": "Cap 1", "canonical_order": 1, "status": "authorized"},
            {"code": "C2", "volume_code": "V1", "title": "Cap 2", "canonical_order": 2, "status": "authorized"},
        ],
        "syllabi": [
            {"chapter_code": "C1", "title": "Ementa 1", "objectives": ["obj"], "program_content": ["pc"], "status": "active", "version": "1.0.0"},
            {"chapter_code": "C2", "title": "Ementa 2", "objectives": ["obj2"], "program_content": ["pc2"], "status": "active", "version": "1.0.0"},
        ],
    }


def create_batch(payload, name="batch1"):
    r = client.post('/api/v1/admin/curriculum/import-batches', json={
        'source_type': 'payload', 'source_name': name, 'imported_by_user_id': 10, 'payload': payload
    })
    return r.json()['id']


def validate_batch(batch_id):
    return client.post(f'/api/v1/admin/curriculum/import-batches/{batch_id}/validate')


def test_01_create_import_batch():
    bid = create_batch(valid_payload())
    detail = client.get(f'/api/v1/admin/curriculum/import-batches/{bid}').json()
    assert detail['status'] == 'uploaded'


def test_02_validate_correct_batch():
    bid = create_batch(valid_payload())
    res = validate_batch(bid).json()
    assert res['status'] == 'preview_ready' and res['report']['errors'] == 0


def test_03_fail_orphan_chapter():
    p = valid_payload()
    p['chapters'][0]['volume_code'] = 'VX'
    bid = create_batch(p)
    res = validate_batch(bid).json()
    issues = client.get(f'/api/v1/admin/curriculum/import-batches/{bid}/issues').json()
    assert res['status'] == 'failed'
    assert any(i['issue_type'] == 'orphan_chapter' for i in issues)


def test_04_detect_duplicate_code():
    p = valid_payload()
    p['chapters'].append(dict(p['chapters'][0]))
    bid = create_batch(p)
    validate_batch(bid)
    issues = client.get(f'/api/v1/admin/curriculum/import-batches/{bid}/issues').json()
    assert any(i['issue_type'] == 'duplicate_code' for i in issues)


def test_05_detect_duplicate_order():
    p = valid_payload()
    p['volumes'].append({"code": "V2", "track_code": "EMBARCADOS", "title": "Volume 2", "canonical_order": 1, "status": "authorized"})
    bid = create_batch(p)
    validate_batch(bid)
    issues = client.get(f'/api/v1/admin/curriculum/import-batches/{bid}/issues').json()
    assert any(i['issue_type'] == 'duplicate_order' for i in issues)


def test_06_stable_checksum_equivalent_payload():
    p1 = valid_payload()
    p2 = json.loads(json.dumps(valid_payload()))
    p2['collection']['name'] = '  Coleção   Rocha  '
    b1 = create_batch(p1, 'a')
    b2 = create_batch(p2, 'b')
    c1 = validate_batch(b1).json()['report']['checksum']
    c2 = validate_batch(b2).json()['report']['checksum']
    assert c1 == c2


def test_07_checksum_changes_on_material_change():
    p1 = valid_payload()
    p2 = valid_payload()
    p2['chapters'][0]['title'] = 'Capítulo alterado'
    b1 = create_batch(p1, 'a')
    b2 = create_batch(p2, 'b')
    c1 = validate_batch(b1).json()['report']['checksum']
    c2 = validate_batch(b2).json()['report']['checksum']
    assert c1 != c2


def test_08_not_apply_failed_batch():
    p = valid_payload(); p['chapters'][0]['volume_code'] = 'VX'
    bid = create_batch(p)
    validate_batch(bid)
    res = client.post(f'/api/v1/admin/curriculum/import-batches/{bid}/apply')
    assert res.status_code == 400


def test_09_apply_valid_batch_register_version():
    bid = create_batch(valid_payload())
    validate_batch(bid)
    ap = client.post(f'/api/v1/admin/curriculum/import-batches/{bid}/apply?applied_by_user_id=7').json()
    assert ap['status'] == 'applied'
    versions = client.get('/api/v1/admin/curriculum/versions').json()
    assert len(versions) >= 1


def test_10_reimport_identical_no_duplicate_structure():
    p = valid_payload()
    b1 = create_batch(p, 'x1'); validate_batch(b1); client.post(f'/api/v1/admin/curriculum/import-batches/{b1}/apply')
    b2 = create_batch(p, 'x2'); validate_batch(b2); ap2 = client.post(f'/api/v1/admin/curriculum/import-batches/{b2}/apply').json()
    assert ap2['idempotent'] is True


def test_11_generation_uses_applied_vigente_base():
    p = valid_payload()
    p['syllabi'][0]['program_content'] = ['novo_conteudo']
    b = create_batch(p); validate_batch(b); client.post(f'/api/v1/admin/curriculum/import-batches/{b}/apply')
    collections = client.get('/api/v1/collections').json()[0]
    tracks = client.get(f"/api/v1/collections/{collections['id']}/tracks").json()[0]
    volumes = client.get(f"/api/v1/tracks/{tracks['id']}/volumes").json()[0]
    req = client.post('/api/v1/generation/requests', json={
        'user_id': 1, 'collection_id': collections['id'], 'track_id': tracks['id'], 'volume_id': volumes['id'], 'chapter_id': 1,
        'piece_kind': 'chapter', 'request_mode': 'create_if_missing', 'prompt_context_json': {}
    }).json()
    assert req['status'] in ['completed', 'reused']


def test_12_issues_endpoint_accessible():
    p = valid_payload(); p['chapters'][0]['volume_code'] = 'VX'
    bid = create_batch(p)
    validate_batch(bid)
    issues = client.get(f'/api/v1/admin/curriculum/import-batches/{bid}/issues')
    assert issues.status_code == 200 and len(issues.json()) > 0


def test_13_preview_exists_before_apply():
    bid = create_batch(valid_payload())
    validate_batch(bid)
    detail = client.get(f'/api/v1/admin/curriculum/import-batches/{bid}').json()
    assert detail['preview_json'] is not None


def test_14_curriculum_change_does_not_delete_generated_pieces():
    # generate piece first
    c = client.get('/api/v1/collections').json()[0]
    t = client.get(f"/api/v1/collections/{c['id']}/tracks").json()[0]
    v = client.get(f"/api/v1/tracks/{t['id']}/volumes").json()[0]
    p_before = client.post('/api/v1/generation/requests', json={
        'user_id': 1, 'collection_id': c['id'], 'track_id': t['id'], 'volume_id': v['id'], 'chapter_id': 1,
        'piece_kind': 'chapter', 'request_mode': 'create_if_missing', 'prompt_context_json': {}
    }).json()['generated_piece']['id']

    bid = create_batch(valid_payload()); validate_batch(bid); client.post(f'/api/v1/admin/curriculum/import-batches/{bid}/apply')
    piece = client.get(f'/api/v1/generated-pieces/{p_before}')
    assert piece.status_code == 200
