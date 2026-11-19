from flask import Blueprint, request, jsonify
from app.errors import DomainError
from app.pipeline import calculate
from app.numeric import serial
from app.store import get_run, append_schedule_event, find_schedule_event, schedule_events
from app.modules.m8_scheduling.service import hydrate, replay

bp = Blueprint('m8', __name__)


@bp.route('/api/m8', methods=['GET', 'POST'])
def module_8():
    if request.method == 'GET':
        ident = request.args.get('run_id')
        if ident:
            return jsonify(_view(ident))
        result = calculate(stop=8)
        return jsonify(serial(result['stages']['8']))
    payload = request.get_json(silent=True) or {}
    result = calculate(payload.get('csv'), stop=8)
    return jsonify(serial(result['stages']['8']))


def _view(ident):
    run = hydrate(get_run(ident))
    stage = run['stages'].get('8')
    if stage is None:
        raise DomainError('NOT_FOUND', '该批次没有连炉排产结果', 404)
    view = stage
    view['run_id'] = ident
    view['rule_version'] = run['manifest']['rule_version']
    return view


def _body():
    body = request.get_json(silent=True) or {}
    ident = body.get('run_id')
    if not ident:
        raise DomainError('REQUEST', '缺少run_id')
    return body, ident


@bp.route('/api/m8/confirm', methods=['POST'])
def confirm():
    body, ident = _body()
    furnace = str(body.get('furnace') or '').strip()
    heat_key = str(body.get('heat_key') or '').strip()
    action = body.get('action')
    if not furnace or not heat_key:
        raise DomainError('REQUEST', '缺少炉座或炉次')
    if action not in ('开炉', '连浇', '清洗', '停等'):
        raise DomainError('DECISION', '确认动作无效')
    run = get_run(ident)
    stage = run['stages'].get('8')
    if stage is None:
        raise DomainError('NOT_FOUND', '该批次没有连炉排产结果', 404)
    key = _key(body, ident, furnace, heat_key, action)
    existing = find_schedule_event(ident, key)
    if existing:
        if existing.get('action') != action:
            raise DomainError('DECISION', '同一幂等键已用于其他动作，请刷新后重试', 409)
        # 重试同一次确认：直接返回既有结果，不重复累计残料。
        view = _view(ident)
        view['event'] = existing
        return jsonify(view)
    current = replay(stage, schedule_events(ident))
    authorized = _authorize(current, furnace, heat_key)
    if action not in authorized:
        raise DomainError('DECISION', f"该炉当前可选动作为：{'/'.join(sorted(authorized))}", 409)
    event = append_schedule_event(ident, {
        'idempotency_key': key,
        'furnace': furnace, 'heat_key': heat_key, 'kind': 'confirm', 'action': action,
        'created_at': _now(),
    })
    view = _view(ident)
    view['event'] = event
    return jsonify(view)


@bp.route('/api/m8/resume', methods=['POST'])
def resume():
    body, ident = _body()
    furnace = str(body.get('furnace') or '').strip()
    heat_key = str(body.get('heat_key') or '').strip()
    if not furnace:
        raise DomainError('REQUEST', '缺少炉座')
    run = get_run(ident)
    stage = run['stages'].get('8')
    if stage is None:
        raise DomainError('NOT_FOUND', '该批次没有连炉排产结果', 404)
    current = replay(stage, schedule_events(ident))
    pending = [key for key, status in current['statuses'].items()
               if status[0] in ('已停等', '需复核') and current['detail'][key]['炉座'] == furnace]
    if not pending:
        raise DomainError('DECISION', '该炉座当前没有可续跑的停等炉次', 409)
    if heat_key and heat_key not in pending:
        raise DomainError('DECISION', '指定炉次不在停等中', 409)
    target = heat_key or pending[0]
    key = _key(body, ident, furnace, target, '续跑')
    existing = find_schedule_event(ident, key)
    if existing:
        view = _view(ident)
        view['event'] = existing
        return jsonify(view)
    event = append_schedule_event(ident, {
        'idempotency_key': key,
        'furnace': furnace, 'heat_key': target, 'kind': 'resume', 'action': None,
        'created_at': _now(),
    })
    view = _view(ident)
    view['event'] = event
    return jsonify(view)


def _authorize(view, furnace, heat_key):
    action = view.get('actions', {}).get(furnace)
    if not action or action.get('kind') != 'confirm' or action.get('heat_key') != heat_key:
        return set()
    return set(action.get('可选动作', []))


def _key(body, ident, furnace, heat_key, action):
    provided = body.get('idempotency_key')
    if provided:
        return str(provided)
    return f"{ident}:{furnace}:{heat_key}:{action}"


def _now():
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
