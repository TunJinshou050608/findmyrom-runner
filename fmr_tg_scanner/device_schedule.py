"""Persistent newest-first rounds with bounded empty-device revisits."""
KEY = 'device_schedule_v1'
EMPTY_DELAYS = (3600, 6 * 3600, 86400, 3 * 86400, 7 * 86400)


def select(ordered, state, now, limit=8):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid device batch size')
    records = state.get('devices', {})
    eligible = [code for code in ordered if records.get(code, {}).get('next_at', 0) <= now]
    seen = set(state.get('seen', []))
    remaining = [code for code in eligible if code not in seen]
    reset = not remaining and bool(eligible)
    return (remaining or eligible)[:limit], reset


def complete(state, targets, report, now, reset=False):
    records = state.setdefault('devices', {})
    seen = set() if reset else set(state.get('seen', []))
    tasks = report.get('tasks_per_device', {})
    funnel = report.get('device_funnel', {})
    for code in targets:
        row = records.setdefault(code, {})
        # Budget exhaustion before this device gets a turn is not an empty probe.
        if tasks and not tasks.get(code) and report.get('outcome') in ('budget_reached','time_budget_reached'):
            continue
        if report.get('incomplete_device') == code:
            continue
        seen.add(code)
        new = funnel.get(code, {}).get('new_products', 0)
        if not report.get('ok', True) or (not new and any(v.get('device') == code for v in report.get('failures', []))):
            row.update(last_at=now, next_at=now + 900, last_outcome='transport_failure')
        elif new:
            row.update(last_at=now, next_at=now + 600, empty_runs=0, last_outcome='new_resources')
        else:
            empty = row.get('empty_runs', 0) + 1
            row.update(last_at=now, next_at=now + EMPTY_DELAYS[min(empty - 1, len(EMPTY_DELAYS) - 1)],
                       empty_runs=empty, last_outcome='no_new_resources')
    state['seen'] = sorted(seen)
    state['round'] = state.get('round', 1) + int(reset)
    return state
