"""连炉排产模块服务：折叠确认事件，挂到流水线。

排产快照（stages.8.heats）在首次运行时由前序阶段产出，并随批次一起持久化。
后续确认只追加事件，用同一份快照重放，因此：
- 重试同一次确认不会重复累计残料（幂等键去重 + 同一炉次只接受一次确认）；
- 输入或规则版本未变时，已开炉批次的依据始终可追回。
"""
from collections import Counter
from app.modules.m8_scheduling.rules import apply, build_heats, load_heats, plan, serialize_heats
from app.numeric import serial


def execute(ctx):
    before = len(ctx['quarantine'])
    result = apply(ctx, ctx.get('schedule_events'))
    result['stage'] = 8
    result['name'] = '连炉排产'
    result['issues'] = dict(Counter(row['原因码'] for row in ctx['quarantine']))
    result['new_quarantine'] = len(ctx['quarantine']) - before
    result['heats'] = serialize_heats(build_heats(ctx)[0])
    ctx['stages'][str(8)] = serial(result)
    return ctx


def replay(stage, events):
    """用已持久化的排产快照重放确认事件，返回与运行结果同构的载荷。"""
    heats = load_heats(stage.get('heats'))
    result = plan(heats, stage.get('unscheduled', 0), events)
    result['stage'] = 8
    result['name'] = '连炉排产'
    result['issues'] = stage.get('issues', {})
    result['new_quarantine'] = stage.get('new_quarantine', 0)
    result['heats'] = stage.get('heats', [])
    result['legacy'] = not stage.get('heats')
    return serial(result)


def hydrate(run):
    """把持久化批次里的第 8 阶段替换为折叠当前事件后的结果（页面与 API 共用）。"""
    from app.store import schedule_events
    stages = run.get('stages') or {}
    stage = stages.get('8')
    if not stage or 'heats' not in stage:
        # 旧规则版本下的批次没有排产快照：给出空方案占位，页面仍可打开。
        if '8' not in stages:
            stages = dict(stages)
            stages['8'] = legacy_stage(run)
        return {**run, 'stages': stages}
    ident = run['manifest']['run_id']
    events = schedule_events(ident)
    view = replay(stage, events)
    stages = dict(stages)
    stages['8'] = view
    return {**run, 'stages': stages}


def legacy_stage(run):
    """为旧批次生成第 8 阶段的只读占位，说明需按当前规则重跑。"""
    return {
        'stage': 8, 'name': '连炉排产', 'rows': [], 'detail': {}, 'furnaces': {},
        'order': [], 'statuses': {}, 'next_heat': {}, 'actions': {}, 'events': [],
        'metrics': [['炉座', 0, '座'], ['排产炉次', 0, '炉'], ['需清洗', 0, '炉'],
                    ['已停等', 0, '炉'], ['待确认', 0, '炉'], ['已开炉', 0, '炉'],
                    ['计划承接残料', '0', 'kg']],
        'note': '该批次由旧规则版本生成，没有连炉排产依据；请用同一 CSV 重新运行以生成方案。',
        'residual_rate': '0', 'unscheduled': 0,
        'rule_version': run.get('manifest', {}).get('rule_version', ''),
        'issues': {}, 'new_quarantine': 0, 'heats': [], 'legacy': True,
    }


