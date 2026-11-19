import io

from app.config import SEQUENCE_SAMPLE


def _run_sequence(client):
    text = SEQUENCE_SAMPLE.read_text(encoding='utf-8-sig')
    return client.post('/api/run', json={'csv': text}).get_json()


def _view(client, ident):
    return client.get('/api/m8?run_id=' + ident).get_json()


def _confirm(client, ident, furnace, heat_key, action):
    return client.post('/api/m8/confirm', json={'run_id': ident, 'furnace': furnace,
                                                'heat_key': heat_key, 'action': action})


def test_page_and_endpoint_expose_plan(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    page = client.get('/work/8?run_id=' + ident).data.decode()
    assert '连炉方案' in page and 'H-102' in page and '承接残料' in page
    view = _view(client, ident)
    assert view['rows'] and view['actions']['F-01']['建议'] == '开炉'


def test_unconfirmed_plan_is_read_only(client):
    run = _run_sequence(client)
    stage = run['stages']['8']
    assert all(row['执行状态'] in ('待确认', '待前序') for row in stage['rows'])
    assert all(row['确认动作'] == '—' for row in stage['rows'])


def test_confirm_records_evidence_and_advances(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    response = _confirm(client, ident, 'F-01', 'F-01|1|H-101', '开炉')
    assert response.status_code == 200
    view = response.get_json()
    row = next(r for r in view['rows'] if r['炉次'] == 'H-101')
    assert row['执行状态'] == '已开炉' and row['确认动作'] == '开炉' and row['确认时间'] != '—'
    assert view['event']['reused'] is False
    assert view['actions']['F-01']['heat_key'] == 'F-01|2|H-102'


def test_retry_same_confirmation_is_idempotent(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    first = _confirm(client, ident, 'F-01', 'F-01|1|H-101', '开炉').get_json()
    second = _confirm(client, ident, 'F-01', 'F-01|1|H-101', '开炉').get_json()
    assert second['event']['reused'] is True
    assert second['rows'] == first['rows']
    heel = {r['炉次']: r['入炉残料_kg'] for r in second['rows']}
    assert heel['H-103'] == '11.7600'


def test_cannot_carry_high_copper_heel_into_low_copper(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    _confirm(client, ident, 'F-01', 'F-01|1|H-101', '开炉')
    blocked = _confirm(client, ident, 'F-01', 'F-01|2|H-102', '连浇')
    assert blocked.status_code == 409
    allowed = _confirm(client, ident, 'F-01', 'F-01|2|H-102', '清洗')
    assert allowed.status_code == 200
    assert allowed.get_json()['event']['action'] == '清洗'


def test_confirmation_for_wrong_heat_is_rejected(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    # F-01 当前只轮到 H-101，不能提前确认 H-103。
    assert _confirm(client, ident, 'F-01', 'F-01|3|H-103', '连浇').status_code == 409
    assert _confirm(client, ident, 'F-99', 'F-99|1|X', '开炉').status_code == 409


def test_stop_and_resume_keeps_furnace_order(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    for key, action in [('F-01|1|H-101', '开炉'), ('F-01|2|H-102', '清洗'),
                        ('F-01|3|H-103', '连浇'), ('F-01|4|H-104', '停等')]:
        assert _confirm(client, ident, 'F-01', key, action).status_code == 200
    stopped = _view(client, ident)
    assert stopped['actions']['F-01']['kind'] == 'resume'
    assert next(r for r in stopped['rows'] if r['炉次'] == 'H-105')['执行状态'] == '等待续跑'

    resumed = client.post('/api/m8/resume', json={'run_id': ident, 'furnace': 'F-01',
                                                  'heat_key': 'F-01|4|H-104'})
    assert resumed.status_code == 200
    view = resumed.get_json()
    order = [r['炉次'] for r in view['rows'] if r['炉座'] == 'F-01']
    assert order == ['H-101', 'H-102', 'H-103', 'H-104', 'H-105']
    assert next(r for r in view['rows'] if r['炉次'] == 'H-105')['执行状态'] == '待确认'


def test_resume_is_idempotent(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    for key, action in [('F-01|1|H-101', '开炉'), ('F-01|2|H-102', '清洗'),
                        ('F-01|3|H-103', '连浇'), ('F-01|4|H-104', '停等')]:
        _confirm(client, ident, 'F-01', key, action)
    first = client.post('/api/m8/resume', json={'run_id': ident, 'furnace': 'F-01'}).get_json()
    second = client.post('/api/m8/resume', json={'run_id': ident, 'furnace': 'F-01'}).get_json()
    assert second['event']['reused'] is True
    assert first['rows'] == second['rows']


def test_resume_without_stop_is_rejected(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    response = client.post('/api/m8/resume', json={'run_id': ident, 'furnace': 'F-01'})
    assert response.status_code == 409


def test_confirm_requires_valid_payload(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    assert client.post('/api/m8/confirm', json={'run_id': ident, 'furnace': 'F-01',
                                                'heat_key': 'F-01|1|H-101',
                                                'action': '乱写'}).status_code == 422
    assert client.post('/api/m8/confirm', json={'furnace': 'F-01'}).status_code == 422
    assert client.post('/api/m8/confirm', json={'run_id': 'absent', 'furnace': 'F-01',
                                                'heat_key': 'x', 'action': '开炉'}).status_code == 404


def test_opened_heat_evidence_is_recoverable(client):
    run = _run_sequence(client)
    ident = run['manifest']['run_id']
    _confirm(client, ident, 'F-01', 'F-01|1|H-101', '开炉')
    view = _view(client, ident)
    row = next(r for r in view['rows'] if r['炉次'] == 'H-101')
    sources = view['detail'][row['key']]['来源行']
    assert {s['行号'] for s in sources} == {2, 3}
    evidence = client.get('/api/evidence.csv?run_id=' + ident).data.decode('utf-8-sig')
    assert 'H-101' in evidence and '已开炉' in evidence


def test_sequence_sample_does_not_break_earlier_modules(client):
    run = _run_sequence(client)
    assert run['stages']['2']['metrics'][0][1] > 0
    assert run['stages']['8']['note'] == ''


def test_legacy_run_without_schedule_stage_degrades_gracefully(client):
    import json
    from app.store import connection
    from app.pipeline import calculate
    legacy = calculate()
    legacy['stages'].pop('8')
    legacy['manifest']['run_id'] = 'legacy0000000000'
    with connection() as db:
        db.execute('INSERT INTO runs(id,payload) VALUES (?,?)',
                   ('legacy0000000000', json.dumps(legacy, ensure_ascii=False)))
    page = client.get('/work/8?run_id=legacy0000000000')
    assert page.status_code == 200 and '旧规则版本' in page.data.decode()
    view = client.get('/api/m8?run_id=legacy0000000000')
    assert view.status_code == 200 and view.get_json()['legacy'] is True
    # 没有依据不允许确认
    assert client.post('/api/m8/confirm', json={'run_id': 'legacy0000000000', 'furnace': 'F',
                                                'heat_key': 'K', 'action': '开炉'}).status_code in (404, 409)
