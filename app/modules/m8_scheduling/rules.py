"""连炉排产：按炉座与炉序推演残料承接、清洗与停等。

模型
----
- 炉次（batch）由可选列 furnace（炉座）、seq（炉序）、grade（牌号）定位；
  三列全空的批次不进入连炉方案。只有部分排产字段、同炉数据冲突或炉序
  重复的炉次一律判为停等，并保留原因。
- 残料是炉座状态而非逐炉累加：每炉出炉后留下 出炉干重 × RESIDUAL_RATE
  的残料，成分为该炉实际出炉成分；下一炉直接开炉时残料计入入炉总量。
  停等或等待不消耗、也不新增残料，续跑沿用同一份炉内残料。
- 若承接后越出牌号区间则给出清洗方案（清除全部承接残料）；若本炉批料
  自身不满足牌号则停等。任何结论都能回溯到原始行号。

事件
----
apply(ctx, events) 的 events 是该批次上按发生顺序记录的确认动作：
  {'kind':'confirm','furnace':F,'heat_key':K,'action':'开炉'|'连浇'|'清洗'|'停等'}
  {'kind':'resume','furnace':F,'heat_key':K}
动作序列是排产结果的唯一真值来源：结果是对动作的纯折叠，重放同一事件
不会重复累计残料；停等后续跑保留原有炉序与炉内残料。
"""
from decimal import Decimal
from app.domain import GRADES, RESIDUAL_RATE, blend
from app.numeric import number, fmt, serial
from app.config import RULE_VERSION

STOP_REASONS = {
    'MISSING_ELEMENT': '本炉缺少牌号所需元素检测，需补测',
    'OWN_ABOVE': '本炉批料成分高于牌号上限，需重新配料',
    'OWN_BELOW': '本炉批料成分低于牌号下限，需重新配料',
    'OWN_UNCERTAIN': '本炉批料区间跨出牌号，需补测确认',
    'META_MISSING': '排产字段不完整',
    'META_CONFLICT': '同一炉次的炉座/炉序/牌号不一致',
    'SEQ_CONFLICT': '同一炉座出现重复炉序',
    'GRADE_UNKNOWN': '牌号未登记',
    'NO_ASSAY': '本炉没有可用的成分记录',
    'PREV_HOLD': '前置炉次停等，本炉等待续跑',
    'UNSCHEDULED': '本炉未提供排产字段',
    'MANUAL_HOLD': '人工确认停等',
}
CLEAN_REASONS = {
    'HIGH_CU_HEEL': '上炉铜高残料会污染低铜炉次',
    'GRADE_HEEL': '上炉残料会使本炉成分超出牌号',
}


def pair(low, high):
    return [fmt(low), fmt(high)]


def heat_content(rows):
    """本炉批料干重与各元素含量区间（%）。"""
    mixture = blend(rows)
    if not mixture:
        return Decimal(0), {}
    total = number(mixture[0]['干重_kg'])
    elems = {r['元素']: (number(r['含量下界_pct']), number(r['含量上界_pct'])) for r in mixture}
    return total, elems


def read_meta(rows):
    """返回 (furnace, seq, grade, code, text)；code 为空表示排产字段可用。"""
    triples = {(r['furnace'].strip(), r['seq'].strip(), r['grade'].strip()) for r in rows}
    if len(triples) != 1:
        return None, None, None, 'META_CONFLICT', STOP_REASONS['META_CONFLICT']
    furnace, seq_text, grade = triples.pop()
    if not (furnace or seq_text or grade):
        return None, None, None, 'UNSCHEDULED', ''
    if not furnace or not seq_text or not grade:
        return furnace or None, None, grade or None, 'META_MISSING', STOP_REASONS['META_MISSING']
    try:
        value = number(seq_text)
    except Exception:
        return furnace, None, grade, 'META_MISSING', '炉序不是有效数值'
    if value != value.to_integral_value() or value < 0:
        return furnace, None, grade, 'META_MISSING', '炉序必须是非负整数'
    seq = int(value)
    if grade not in GRADES:
        return furnace, seq, grade, 'GRADE_UNKNOWN', f"牌号{grade}未登记"
    return furnace, seq, grade, '', ''


def own_check(grade, elems, own_mass):
    """判本炉批料自身能否满足牌号，返回 (是否通过, 原因码, 说明)。"""
    if own_mass <= 0 or not elems:
        return False, 'NO_ASSAY', STOP_REASONS['NO_ASSAY']
    for element, (low, high) in GRADES[grade].items():
        if element not in elems:
            return False, 'MISSING_ELEMENT', f"本炉缺少{element}检测，无法确认{grade}成分"
        current_low, current_high = elems[element]
        if current_low > high:
            return False, 'OWN_ABOVE', f"本炉{element}下界{fmt(current_low)}%高于{grade}上限{fmt(high)}%"
        if current_high < low:
            return False, 'OWN_BELOW', f"本炉{element}上界{fmt(current_high)}%低于{grade}下限{fmt(low)}%"
        if current_low < low or current_high > high:
            return False, 'OWN_UNCERTAIN', (f"本炉{element}区间{fmt(current_low)}–{fmt(current_high)}%"
                                            f"跨出{grade}区间{fmt(low)}–{fmt(high)}%")
    return True, '', ''


def carry(own_mass, own_elems, residual_mass, residual_elems):
    total = own_mass + residual_mass
    carried = {}
    for element, (own_low, own_high) in own_elems.items():
        heel_low, heel_high = residual_elems.get(element, (Decimal(0), Decimal(100)))
        carried[element] = ((own_mass * own_low + residual_mass * heel_low) / total,
                            (own_mass * own_high + residual_mass * heel_high) / total)
    return carried


def violation(grade, carried):
    for element, (low, high) in GRADES[grade].items():
        if element not in carried:
            return element, None
        current_low, current_high = carried[element]
        if current_high > high or current_low < low:
            return element, (current_low, current_high, low, high)
    return None, None


def element_rows(grade, own, carried, simulated=None):
    rows = {}
    for element, (low, high) in GRADES[grade].items():
        current = own.get(element)
        after = carried.get(element) if carried else None
        if current is None:
            rows[element] = {'批料_pct': ['—', '—'], '承接后_pct': ['—', '—'],
                            '牌号区间_pct': pair(low, high), '判定': '缺测', '变化': '—'}
            continue
        if current[0] > high:
            verdict = '批料超上限'
        elif current[1] < low:
            verdict = '批料低于下限'
        elif current[0] < low or current[1] > high:
            verdict = '批料需补测'
        else:
            verdict = '批料合格'
        after_pair = pair(*after) if after is not None else ['—', '—']
        if after is not None:
            if after[1] > high:
                verdict = '承接后超上限'
            elif after[0] < low:
                verdict = '承接后低于下限'
        change = f"{fmt(current[0])}→{fmt(after[0])}" if after is not None else '—'
        row = {'批料_pct': pair(*current), '承接后_pct': after_pair,
               '牌号区间_pct': pair(low, high), '判定': verdict, '变化': change}
        if simulated is not None and element in simulated:
            row['未清洗时_pct'] = pair(*simulated[element])
        rows[element] = row
    return rows


def source_rows(rows):
    return [{'行号': r['行号'], '批号': r['lot'], '元素': r['element'], '检测值': r['assay'],
             '干湿基': r['basis'], '水分': r['moisture'], '干重_kg': r['dry_mass']} for r in rows]


def heat_key(furnace, seq, batch):
    return f"{furnace if furnace else '—'}|{seq if seq is not None else '—'}|{batch}"


def blank_state():
    return {'heel': Decimal(0), 'elems': None, 'last': None}


def _detail(heat, decision, state, inherited, ingress, removed, total_mass, heel,
            elements, clean_code='', clean_text='', stop_code='', stop_text=''):
    key = heat_key(heat['furnace'], heat['seq'], heat['batch'])
    return {
        'key': key,
        '炉座': heat['furnace'] if heat['furnace'] else '—',
        '炉序': str(heat['seq']) if heat['seq'] is not None else '—',
        '炉次': heat['batch'],
        '牌号': heat['grade'] or '—',
        '判定': decision,
        '上一炉': ({'炉次': state['last']['batch'], '牌号': state['last']['grade'],
                   '出炉残料_kg': fmt(state['heel'])} if state['last'] else None),
        '上炉残料_kg': fmt(state['heel']) if state['last'] else '0.000',
        '承接残料_kg': fmt(inherited),
        '清洗去除_kg': fmt(removed),
        '入炉残料_kg': fmt(ingress),
        '炉料干重_kg': fmt(heat['own_mass']),
        '入炉总量_kg': fmt(total_mass),
        '停等前批料干重_kg': fmt(heat['own_mass']),
        '出炉残料_kg': fmt(heel),
        '残料系数': str(RESIDUAL_RATE),
        '清洗': decision == '清洗',
        '清洗原因码': clean_code,
        '清洗原因': clean_text,
        '停等原因码': stop_code,
        '停等原因': stop_text,
        '成分': ({e: v for e, v in elements.items()} if heat['grade'] in GRADES else {}),
        '来源行': source_rows(heat['rows']),
    }


def _hold(heat, state, code, text, decision='停等'):
    # 停等/等待没有实际入炉：入炉总量记 0，批料干重单独保留为参考。
    detail = _detail(heat, decision, state, Decimal(0), Decimal(0), Decimal(0),
                     Decimal(0), Decimal(0),
                     element_rows(heat['grade'], heat['elems'], None) if heat['grade'] in GRADES else {},
                     stop_code=code, stop_text=text)
    detail['停等前批料干重_kg'] = fmt(heat['own_mass'])
    if decision == '等待':
        detail['停等原因码'] = 'PREV_HOLD'
        detail['停等原因'] = STOP_REASONS['PREV_HOLD']
    return detail


def execute(heat, state, forced=None):
    """执行/推演一炉。返回 (detail, new_state, executed)。"""
    if heat['furnace'] is None or heat['seq'] is None or heat['grade'] not in GRADES:
        code = heat['stop_code'] or 'META_MISSING'
        text = heat['stop_text'] or STOP_REASONS.get(code, '排产依据不足')
        return _hold(heat, state, code, text), state, False
    if heat['stop_code']:
        return _hold(heat, state, heat['stop_code'], heat['stop_text']), state, False
    if forced == '停等':
        # 保留真实停等依据；没有依据时归为人工停等。
        own_ok, own_code, own_text = own_check(heat['grade'], heat['elems'], heat['own_mass'])
        if not own_ok:
            return _hold(heat, state, own_code, own_text), state, False
        return _hold(heat, state, 'MANUAL_HOLD', STOP_REASONS['MANUAL_HOLD']), state, False

    inherited = state['heel'] if state['last'] else Decimal(0)
    own_ok, own_code, own_text = own_check(heat['grade'], heat['elems'], heat['own_mass'])
    if not own_ok:
        return _hold(heat, state, own_code, own_text), state, False

    clean_code = clean_text = ''
    simulated = None
    if inherited <= 0:
        decision, ingress, removed = '开炉', Decimal(0), Decimal(0)
        carried = heat['elems']
    else:
        simulated = carry(heat['own_mass'], heat['elems'], inherited, state['elems'])
        hit, values = violation(heat['grade'], simulated)
        # 只有在承接后不越出牌号时才允许连浇；确认动作不能放宽这条底线。
        if forced != '清洗' and not hit:
            decision, ingress, removed = '连浇', inherited, Decimal(0)
            carried = simulated
        elif forced == '连浇' and not hit:
            decision, ingress, removed = '连浇', inherited, Decimal(0)
            carried = simulated
        else:
            decision, ingress, removed = '清洗', Decimal(0), inherited
            carried = heat['elems']
            if hit:
                element = hit
                low, high, limit_low, limit_high = values
                prev_cu = state['elems'].get('Cu') if state['elems'] else None
                if (element == 'Cu' and prev_cu is not None
                        and prev_cu[0] > heat['elems'].get('Cu', (Decimal(0),))[0]):
                    clean_code = 'HIGH_CU_HEEL'
                    clean_text = (f"上炉{state['last']['batch']}({state['last']['grade']})残料"
                                  f"{fmt(inherited)}kg含Cu约{fmt(prev_cu[0])}%，使本炉Cu上界"
                                  f"{fmt(high)}%超出{heat['grade']}上限{fmt(limit_high)}%")
                else:
                    clean_code = 'GRADE_HEEL'
                    clean_text = (f"上炉{state['last']['batch']}({state['last']['grade']})残料"
                                  f"{fmt(inherited)}kg使本炉{element}{fmt(low)}–{fmt(high)}%"
                                  f"超出{heat['grade']}区间{fmt(limit_low)}–{fmt(limit_high)}%")
            else:
                clean_code = 'GRADE_HEEL'
                clean_text = f"人工确认清洗，去除上炉残料{fmt(inherited)}kg"

    total_mass = heat['own_mass'] + ingress
    heel = total_mass * RESIDUAL_RATE
    elements = element_rows(heat['grade'], heat['elems'], carried, simulated if decision == '清洗' else None)
    detail = _detail(heat, decision, state, inherited, ingress, removed, total_mass, heel, elements,
                     clean_code=clean_code, clean_text=clean_text)
    new_state = {'heel': heel, 'elems': carried,
                 'last': {'batch': heat['batch'], 'grade': heat['grade']}}
    return detail, new_state, True


def authorize(planned):
    """确认动作的合法集合。计划判定的定性结论不可被放宽：
    需要清洗的炉次不允许直接连浇，避免铜高残料进低铜炉次。"""
    if planned == '停等':
        return {'停等'}
    if planned == '等待':
        return set()
    if planned == '开炉':
        return {'开炉', '停等'}
    if planned == '清洗':
        return {'清洗', '停等'}
    return {'连浇', '清洗', '停等'}


def evidence_text(detail):
    """把本炉来源行压成可复核的一行依据：行号与各元素检测值。"""
    parts = []
    for row in detail['来源行']:
        parts.append(f"行{row['行号']}:{row['批号']}/{row['元素']}={row['检测值']}"
                     f"({row['干湿基']},水{row['水分']}%,干重{row['干重_kg']}kg)")
    return '；'.join(parts)


def flat(detail, status, planned, action='', confirmed_at=''):
    row = {
        'key': detail['key'], '炉座': detail['炉座'], '炉序': detail['炉序'],
        '炉次': detail['炉次'], '牌号': detail['牌号'],
        '上一炉': detail['上一炉']['炉次'] if detail['上一炉'] else '—',
        '承接残料_kg': detail['承接残料_kg'], '清洗': '是' if detail['清洗'] else '否',
        '清洗原因码': detail['清洗原因码'], '清洗原因': detail['清洗原因'],
        '入炉残料_kg': detail['入炉残料_kg'], '清洗去除_kg': detail['清洗去除_kg'],
        '入炉总量_kg': detail['入炉总量_kg'], '出炉残料_kg': detail['出炉残料_kg'],
        '停等前批料干重_kg': detail.get('停等前批料干重_kg', '—'),
        '计划判定': planned, '执行状态': status, '确认动作': action or '—',
        '确认时间': confirmed_at or '—',
        '判定': detail['判定'], '停等原因码': detail['停等原因码'], '停等原因': detail['停等原因'],
        '依据': evidence_text(detail),
    }
    for element in ('Cu', 'Zn'):
        change = detail['成分'].get(element, {})
        row[f'{element}_批料_pct'] = '–'.join(change.get('批料_pct', ['—', '—']))
        row[f'{element}_承接后_pct'] = '–'.join(change.get('承接后_pct', ['—', '—']))
        row[f'{element}_牌号_pct'] = '–'.join(change.get('牌号区间_pct', ['—', '—']))
        row[f'{element}_变化'] = change.get('变化', '—')
        if change.get('未清洗时_pct'):
            row[f'{element}_未清洗_pct'] = '–'.join(change['未清洗时_pct'])
    return row


def _events_index(events):
    """按发生顺序折叠事件：同一炉次只采纳第一次确认，之后的确认视为重复。
    这样重试、重复提交或并发追加都不会改变结果。"""
    confirmations = {}
    resumes = set()
    for event in events or []:
        if event.get('kind') == 'confirm':
            confirmations.setdefault(event['heat_key'], event)
        elif event.get('kind') == 'resume':
            resumes.add(event['heat_key'])
    return confirmations, resumes


def plan(heats, unscheduled, events=None):
    confirmations, resumes = _events_index(events)
    furnaces = {}
    details = {}
    statuses = {}
    confirmed = {}
    confirmed_at = {}
    next_heat = {}
    pending_resume = {}
    events_view = []

    furniture = sorted({h['furnace'] for h in heats if h['furnace'] and h['seq'] is not None})
    for furnace in furniture:
        order = sorted([h for h in heats if h['furnace'] == furnace and h['seq'] is not None],
                       key=lambda h: (h['seq'], h['batch']))
        furnaces[furnace] = [heat_key(h['furnace'], h['seq'], h['batch']) for h in order]
        state = blank_state()
        blocked_by = None
        next_heat[furnace] = None
        for heat in order:
            key = heat_key(heat['furnace'], heat['seq'], heat['batch'])
            if blocked_by is not None and blocked_by in resumes:
                blocked_by = None
            if blocked_by is not None:
                if heat['stop_code']:
                    # 本炉自身已有停等依据（如重复炉序），不被“等待”覆盖。
                    detail = _hold(heat, state, heat['stop_code'], heat['stop_text'])
                else:
                    detail = _hold(heat, state, 'PREV_HOLD', STOP_REASONS['PREV_HOLD'], decision='等待')
                    detail['计划判定'] = '等待'
                details[key] = detail
                status = '等待续跑' if blocked_by in confirmed else '待前序'
                statuses[key] = (status, detail['判定'], '')
                events_view.append({'事件': '等待', '炉座': furnace, '炉次': heat['batch']})
                continue
            event = confirmations.get(key)
            if event:
                planned_detail, _, _ = execute(heat, state, forced=None)
                allowed = authorize(planned_detail['判定'])
                action = event['action']
                # 越出许可的确认（例如该清洗却记录连浇）仍按安全方向执行：
                # execute 自身不会让越界残料进炉，这里只负责标注需复核。
                detail, state_after, ran = execute(heat, state, forced=action)
                downgraded = action not in allowed or detail['判定'] != action
                if not ran:
                    if action == '停等':
                        status = '已停等'
                        blocked_by = key
                    else:
                        if detail['停等原因码'] != 'MANUAL_HOLD':
                            detail['停等原因'] = (detail['停等原因'] or '') + '（确认依据已变化，请复核）'
                        blocked_by = key
                        status = '需复核'
                else:
                    status = {'开炉': '已开炉', '连浇': '已开炉', '清洗': '已清洗', '停等': '已停等'}[action]
                    if action == '停等':
                        blocked_by = key
                    else:
                        state = state_after
                    if downgraded:
                        status = '需复核'
                        detail['停等原因码'] = detail['停等原因码'] or 'CONFIRM_STALE'
                        detail['停等原因'] = (detail['停等原因'] or '') + f'（确认动作{action}已按安全方案执行，请复核）'
                details[key] = detail
                confirmed[key] = action
                confirmed_at[key] = event.get('created_at', '')
                statuses[key] = (status, planned_detail['判定'], action)
                events_view.append({'事件': '确认', '炉座': furnace, '炉次': heat['batch'],
                                    '动作': action, '时间': event.get('created_at', '')})
                continue
            detail, state_after, ran = execute(heat, state, forced=None)
            planned = detail['判定']
            status = '待确认' if next_heat[furnace] is None else '待前序'
            if next_heat[furnace] is None:
                next_heat[furnace] = key
            details[key] = detail
            statuses[key] = (status, planned, '')
            state = state_after
            if planned == '停等':
                blocked_by = key
            events_view.append({'事件': '计划', '炉座': furnace, '炉次': heat['batch'],
                                '建议': planned})
        if blocked_by is not None and next_heat[furnace] is None:
            pending_resume[furnace] = blocked_by

    # 无法定位炉序的炉次也进入方案，明确停等并保留追回入口。
    extras = []
    for heat in heats:
        key = heat_key(heat['furnace'], heat['seq'], heat['batch'])
        if key in details:
            continue
        detail, _, _ = execute(heat, blank_state(), forced=None)
        detail['判定'] = '停等'
        detail['停等原因码'] = heat['stop_code'] or 'NO_ASSAY'
        detail['停等原因'] = heat['stop_text'] or STOP_REASONS['NO_ASSAY']
        details[key] = detail
        # 排产字段缺失/牌号未登记的炉次无法定位到炉座炉序，标为待补排产。
        statuses[key] = ('待补排产', '停等', '')
        events_view.append({'事件': '计划', '炉座': heat['furnace'] or '—', '炉次': heat['batch'],
                            '建议': '停等'})
        extras.append(key)

    order = [key for keys in furnaces.values() for key in keys] + sorted(extras)
    rows = [flat(details[key], statuses[key][0], statuses[key][1], statuses[key][2],
                 confirmed_at.get(key, '')) for key in order]

    # 每座炉当前可执行的一步：待确认动作，或停等后续跑。
    actions = {}
    for furnace in furniture:
        if next_heat.get(furnace):
            key = next_heat[furnace]
            detail = details[key]
            actions[furnace] = {
                'kind': 'confirm', 'heat_key': key, '炉次': detail['炉次'],
                '牌号': detail['牌号'], '建议': detail['判定'],
                '可选动作': sorted(authorize(detail['判定'])),
                '承接残料_kg': detail['承接残料_kg'],
                '清洗原因': detail['清洗原因'],
                '停等原因': detail['停等原因'],
            }
        elif pending_resume.get(furnace):
            key = pending_resume[furnace]
            actions[furnace] = {'kind': 'resume', 'heat_key': key,
                                '炉次': details[key]['炉次'], '牌号': details[key]['牌号'],
                                '建议': '续跑'}

    ingress_total = sum((Decimal(details[key]['入炉残料_kg']) for key in order), Decimal(0))
    metrics = [
        ['炉座', len(furnaces), '座'],
        ['排产炉次', len([h for h in heats if h['furnace'] and h['seq'] is not None]), '炉'],
        ['需清洗', sum(1 for key in order if details[key]['清洗']), '炉'],
        ['已停等', sum(1 for key in order if statuses[key][0] == '已停等'), '炉'],
        ['待确认', sum(1 for key in order if statuses[key][0] == '待确认'), '炉'],
        ['已开炉', sum(1 for key in order if statuses[key][0] in ('已开炉', '已清洗')), '炉'],
        ['计划承接残料', fmt(ingress_total), 'kg'],
    ]
    note = '' if heats else '输入缺少 furnace/seq/grade 排产列，未生成连炉方案。'
    return {'rows': rows, 'detail': details, 'furnaces': furnaces, 'order': order,
            'statuses': statuses, 'next_heat': next_heat, 'actions': actions,
            'events': events_view, 'metrics': metrics, 'note': note,
            'residual_rate': str(RESIDUAL_RATE), 'unscheduled': unscheduled,
            'rule_version': RULE_VERSION,
            'cleaned': sum(1 for key in order if details[key]['清洗']),
            'held': sum(1 for key in order if statuses[key][0] == '已停等'),
            'waiting': sum(1 for key in order if statuses[key][0] == '等待续跑'),
            'review_needed': sum(1 for key in order if statuses[key][0] == '需复核')}


def build_heats(ctx):
    batches = {}
    for row in ctx['eligible']:
        batches.setdefault(row['batch'], []).append(row)
    heats = []
    unscheduled = 0
    for batch, rows in batches.items():
        own_mass, elems = heat_content(rows)
        furnace, seq, grade, code, text = read_meta(rows)
        if code == 'UNSCHEDULED':
            unscheduled += 1
            continue
        heats.append({'batch': batch, 'rows': rows, 'own_mass': own_mass, 'elems': elems,
                      'furnace': furnace, 'seq': seq, 'grade': grade,
                      'stop_code': code, 'stop_text': text})
    slots = {}
    for heat in heats:
        if heat['furnace'] and heat['seq'] is not None and not heat['stop_code']:
            slots.setdefault((heat['furnace'], heat['seq']), []).append(heat)
    for (furnace, seq), group in slots.items():
        occupied = sorted({h['batch'] for h in group})
        if len(occupied) > 1:
            for heat in group:
                heat['stop_code'] = 'SEQ_CONFLICT'
                heat['stop_text'] = f"炉座{furnace}炉序{seq}被多个炉次占用：" + '、'.join(occupied)
    return heats, unscheduled


def serialize_heats(heats):
    return serial(heats)


def load_heats(stored):
    heats = []
    for heat in stored or []:
        elems = {element: (number(bounds[0]), number(bounds[1]))
                 for element, bounds in (heat.get('elems') or {}).items()}
        heats.append({**heat, 'own_mass': number(heat['own_mass']), 'elems': elems,
                      'rows': [dict(r) for r in heat['rows']]})
    return heats


def apply(ctx, events=None):
    heats, unscheduled = build_heats(ctx)
    return plan(heats, unscheduled, events)
