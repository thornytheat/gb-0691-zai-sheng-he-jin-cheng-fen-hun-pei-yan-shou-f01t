import io
import pytest
from app.config import SEQUENCE_SAMPLE
from app.manifest import make
from app.pipeline import calculate
from app.modules.m1_receiving.service import execute as stage1
from app.modules.m2_assay.service import execute as stage2
from app.modules.m3_recipe.service import execute as stage3
from app.modules.m8_scheduling import rules


@pytest.fixture
def sequence(): return SEQUENCE_SAMPLE.read_text(encoding='utf-8-sig')


@pytest.fixture
def heats(sequence):
    ctx = {'text': sequence, 'rows': [], 'quarantine': [], 'stages': {}, 'manifest': make(sequence)}
    for stage in (stage1, stage2, stage3):
        stage(ctx)
    return rules.build_heats(ctx)


def _confirm(furnace, key, action):
    return {'kind': 'confirm', 'furnace': furnace, 'heat_key': key, 'action': action, 'created_at': 't'}


def _resume(furnace, key):
    return {'kind': 'resume', 'furnace': furnace, 'heat_key': key, 'created_at': 't'}


def _run(heats):
    batch, unscheduled = heats
    return rules.plan(batch, unscheduled, [])


def test_sequence_sample_plan_visible(sequence):
    stage = calculate(sequence)['stages']['8']
    assert stage['note'] == ''
    assert len(stage['rows']) == 11
    statuses = {row['炉次']: row['执行状态'] for row in stage['rows']}
    assert statuses['H-101'] == '待确认'
    assert statuses['H-102'] == '待前序'


def test_sample_without_schedule_columns_is_empty():
    stage = calculate()['stages']['8']
    assert stage['rows'] == [] and stage['unscheduled'] > 0
    assert 'furnace' in stage['note']


def test_carry_math_is_weighted(heats):
    plans = _run(heats)
    h101 = next(r for r in plans['rows'] if r['炉次'] == 'H-101')
    h102 = next(r for r in plans['rows'] if r['炉次'] == 'H-102')
    # H-101: 1000kg 湿基 2% 水 -> 980kg 干基，出炉残料 = 980 * 0.06 = 58.8kg
    assert h101['出炉残料_kg'] == '58.8000'
    assert h101['计划判定'] == '开炉'
    assert h102['承接残料_kg'] == '58.8000'
    # H-102 被判定清洗：承接的 58.8kg 只用于给出清洗依据，不进入炉料；
    # 清洗后出炉残料按本炉批料 196kg 计，为 11.76kg，由 H-103 承接。
    assert h102['入炉残料_kg'] == '0.0000'
    h103 = next(r for r in plans['rows'] if r['炉次'] == 'H-103')
    assert h103['入炉残料_kg'] == '11.7600'
    # H-103 批料 294kg Cu 9% + 11.76kg Cu 7.5%： (294*9+11.76*7.5)/305.76 = 8.9423
    assert h103['Cu_承接后_pct'].startswith('8.9423')


def test_high_copper_heel_forces_cleaning(heats):
    plans = _run(heats)
    h102 = next(r for r in plans['rows'] if r['炉次'] == 'H-102')
    assert h102['计划判定'] == '清洗'
    assert h102['清洗原因码'] == 'HIGH_CU_HEEL'
    assert 'Cu' in h102['清洗原因'] and 'H-101' in h102['清洗原因']
    assert h102['清洗去除_kg'] == h102['承接残料_kg']
    assert h102['入炉残料_kg'] == '0.0000'


def test_clean_heat_produces_no_heel_from_removed_material(heats):
    plans = _run(heats)
    h102 = next(r for r in plans['rows'] if r['炉次'] == 'H-102')
    # 清洗后入炉总量只有本炉批料 196kg 干基，出炉残料据此计算。
    assert h102['入炉总量_kg'] == '196.0000'
    assert h102['出炉残料_kg'] == '11.7600'


def test_own_batch_failure_stops_heat(heats):
    plans = _run(heats)
    h104 = next(r for r in plans['rows'] if r['炉次'] == 'H-104')
    assert h104['计划判定'] == '停等'
    assert h104['停等原因码'] == 'OWN_ABOVE'
    assert 'Cu' in h104['停等原因']


def test_after_stop_is_waiting_and_keeps_order(heats):
    plans = _run(heats)
    order = [r['炉次'] for r in plans['rows'] if r['炉座'] == 'F-01']
    assert order == ['H-101', 'H-102', 'H-103', 'H-104', 'H-105']
    h105 = next(r for r in plans['rows'] if r['炉次'] == 'H-105')
    assert h105['执行状态'] == '待前序' and h105['计划判定'] == '等待'


def test_only_first_heat_of_each_furnace_is_actionable(heats):
    plans = _run(heats)
    assert set(plans['actions']) == {'F-01', 'F-02', 'F-03'}
    for action in plans['actions'].values():
        assert action['kind'] == 'confirm'


def test_confirm_executes_and_advances(heats):
    batch, unscheduled = heats
    first = rules.plan(batch, unscheduled, [])['actions']['F-01']
    events = [_confirm('F-01', first['heat_key'], '开炉')]
    view = rules.plan(batch, unscheduled, events)
    row = next(r for r in view['rows'] if r['炉次'] == 'H-101')
    assert row['执行状态'] == '已开炉' and row['确认动作'] == '开炉'
    assert view['actions']['F-01']['heat_key'] == 'F-01|2|H-102'
    assert view['actions']['F-01']['建议'] == '清洗'


def test_retrying_same_confirmation_does_not_accumulate_heel(heats):
    batch, unscheduled = heats
    first = rules.plan(batch, unscheduled, [])['actions']['F-01']
    once = [_confirm('F-01', first['heat_key'], '开炉')]
    twice = once + [_confirm('F-01', first['heat_key'], '开炉')]
    a = rules.plan(batch, unscheduled, once)
    b = rules.plan(batch, unscheduled, twice)
    assert a['rows'] == b['rows']
    assert next(r for r in b['rows'] if r['炉次'] == 'H-103')['入炉残料_kg'] == '11.7600'


def test_cleaning_heat_cannot_be_confirmed_as_carry(heats):
    batch, unscheduled = heats
    first = rules.plan(batch, unscheduled, [])['actions']['F-01']
    events = [_confirm('F-01', first['heat_key'], '开炉')]
    view = rules.plan(batch, unscheduled, events)
    action = view['actions']['F-01']
    assert '连浇' not in action['可选动作']
    assert set(action['可选动作']) == {'清洗', '停等'}


def test_stop_then_resume_keeps_heel_and_order(heats):
    batch, unscheduled = heats
    keys = ['F-01|1|H-101', 'F-01|2|H-102', 'F-01|3|H-103', 'F-01|4|H-104']
    events = [_confirm('F-01', keys[0], '开炉'), _confirm('F-01', keys[1], '清洗'),
              _confirm('F-01', keys[2], '连浇'), _confirm('F-01', keys[3], '停等')]
    stopped = rules.plan(batch, unscheduled, events)
    h104 = next(r for r in stopped['rows'] if r['炉次'] == 'H-104')
    assert h104['执行状态'] == '已停等' and h104['停等原因码'] == 'OWN_ABOVE'
    h105 = next(r for r in stopped['rows'] if r['炉次'] == 'H-105')
    assert h105['执行状态'] == '等待续跑'
    assert stopped['actions']['F-01']['kind'] == 'resume'

    resumed = rules.plan(batch, unscheduled, events + [_resume('F-01', keys[3])])
    h105 = next(r for r in resumed['rows'] if r['炉次'] == 'H-105')
    assert h105['执行状态'] == '待确认'
    # 停等炉次不产生新残料，H-105 承接的仍是 H-103 的炉内残料。
    assert h105['承接残料_kg'] == '18.3456'
    assert [r['炉次'] for r in resumed['rows'] if r['炉座'] == 'F-01'] == \
        ['H-101', 'H-102', 'H-103', 'H-104', 'H-105']


def test_stale_confirmation_is_flagged_not_silently_applied(heats):
    batch, unscheduled = heats
    # 直接为 H-102 确认连浇，但按当前依据该炉应清洗。
    events = [_confirm('F-01', 'F-01|2|H-102', '连浇')]
    view = rules.plan(batch, unscheduled, events)
    row = next(r for r in view['rows'] if r['炉次'] == 'H-102')
    assert row['执行状态'] == '需复核'
    assert row['入炉残料_kg'] == '0.0000'


def test_duplicate_sequence_stops_whole_slot(sequence):
    lines = sequence.strip().splitlines()
    extra = 'L999,H-999,100,kg,2,Cu,9,dry,1,F-02,1,CU70'
    text = '\n'.join(lines + [extra]) + '\n'
    stage = calculate(text)['stages']['8']
    conflicted = [r for r in stage['rows'] if r['停等原因码'] == 'SEQ_CONFLICT']
    assert {r['炉次'] for r in conflicted} == {'H-201', 'H-999'}
    statuses = {r['炉次']: r['执行状态'] for r in conflicted}
    assert statuses['H-201'] == '待确认' and statuses['H-999'] == '待前序'
    assert all(r['计划判定'] == '停等' for r in conflicted)


def test_missing_meta_stops_without_crashing(sequence):
    lines = sequence.strip().splitlines()
    text = '\n'.join(lines + ['L998,H-998,100,kg,2,Cu,9,dry,1,F-09,,CU70']) + '\n'
    stage = calculate(text)['stages']['8']
    row = next(r for r in stage['rows'] if r['炉次'] == 'H-998')
    assert row['计划判定'] == '停等' and row['停等原因码'] == 'META_MISSING'


def test_evidence_keeps_source_rows(heats):
    stage = calculate(SEQUENCE_SAMPLE.read_text(encoding='utf-8-sig'))['stages']['8']
    h101 = next(r for r in stage['rows'] if r['炉次'] == 'H-101')
    sources = stage['detail'][h101['key']]['来源行']
    assert {r['行号'] for r in sources} == {2, 3}
    assert all('检测值' in r and '干重_kg' in r for r in sources)


def test_carry_guard_holds_even_if_forced(heats):
    """即使事件里出现越界连浇，执行层也不会把铜高残料带入低铜炉次。"""
    batch, unscheduled = heats
    events = [_confirm('F-01', 'F-01|1|H-101', '开炉'),
              _confirm('F-01', 'F-01|2|H-102', '连浇')]
    view = rules.plan(batch, unscheduled, events)
    h102 = next(r for r in view['rows'] if r['炉次'] == 'H-102')
    assert h102['判定'] == '清洗'
    assert h102['入炉残料_kg'] == '0.0000'
    assert h102['执行状态'] == '需复核'
    assert '安全方案' in h102['停等原因']
