#!/usr/bin/env python3
"""Reversible management of this installation's automatic entries only.

No trust-store edits, source deletion, project edits, task-history deletion or
whole-file restoration. All changes require --apply; status is read-only.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import time


def require(ok, message):
    if not ok:
        raise ValueError(message)


def safe(path):
    path = Path(os.path.abspath(path))
    for p in [*reversed(path.parents), path]:
        require(not p.is_symlink(), 'Symbolic link is not a managed file: ' + str(p))
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    path = safe(path)
    require(path.stat().st_size <= 2 * 1024 * 1024, 'Managed metadata exceeds limit')
    return json.loads(path.read_text())


def write(path, value):
    path = safe(path)
    data = (json.dumps(value, ensure_ascii=True, indent=2) + '\n').encode()
    fd, tmp = tempfile.mkstemp(prefix='.manage-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def snapshot(path):
    safe(path)
    return path.read_bytes() if path.exists() else None


def config(path):
    value = read(path) if path.exists() else {'hooks': {}}
    require(isinstance(value, dict) and isinstance(value.get('hooks', {}), dict), 'Invalid hook configuration')
    require(all(isinstance(v, list) for v in value.get('hooks', {}).values()), 'Invalid hook group')
    return value


def definitions(receipt, current, raw):
    groups = receipt.get('managed_groups')
    if groups is None:
        # Legacy installations are adopted only against the exact original file.
        require(raw is not None and digest(raw) == receipt['hooks_sha256'],
                'Legacy receipt/configuration changed; review ownership before managing')
        groups = {}
        handler = next(x['path'] for x in receipt['pinned_files'] if Path(x['path']).name == 'task_hooks.py')
        for event, entries in current.get('hooks', {}).items():
            for entry in entries:
                hooks = entry.get('hooks', [])
                if len(hooks) != 1:
                    continue
                args = shlex.split(hooks[0].get('command', ''))
                if handler in args and '--policy' in args and args[args.index('--policy') + 1] == receipt['policy']:
                    groups.setdefault(event, []).append(copy.deepcopy(entry))
        require(len(groups) == 5 and all(len(x) == 1 for x in groups.values()), 'Incomplete legacy ownership receipt')
    require(isinstance(groups, dict) and set(groups) == {'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'Interrupt'}
            and all(isinstance(x, list) and len(x) == 1 for x in groups.values()), 'Invalid managed definitions')
    return groups


def verify_version(receipt):
    policy = safe(receipt['policy'])
    require(digest(policy.read_bytes()) == receipt['policy_sha256'], 'Policy version changed; reinstall after acceptance')
    for entry in receipt['pinned_files']:
        require(digest(safe(entry['path']).read_bytes()) == entry['sha256'], 'Source changed; reinstall after acceptance')
    evidence = receipt['validation']
    require(digest(safe(evidence['path']).read_bytes()) == evidence['sha256'], 'Acceptance evidence changed')


def _manage(home, action='status', apply=False):
    home = safe(home); base = safe(home / 'task-hooks')
    receipt_path = safe(base / 'installation.json'); cfg_path = safe(home / 'hooks.json')
    state_path = safe(base / 'management.json')
    require(receipt_path.is_file(), 'Installation receipt required; nothing was changed')
    receipt = read(receipt_path); before = snapshot(cfg_path); current = config(cfg_path)
    state = read(state_path) if state_path.exists() else None
    require(state is None or state['installation_policy'] == receipt['policy_sha256'], 'A newer installation supersedes this management receipt')
    groups = state['managed_groups'] if state else definitions(receipt, current, before)
    expected = [(event, group) for event, values in groups.items() for group in values]
    found = [(e, g) for e, g in expected if g in current.get('hooks', {}).get(e, [])]
    # Changed entries for this exact policy must not be silently taken over.
    for event, entries in current.get('hooks', {}).items():
        for group in entries:
            for hook in group.get('hooks', []):
                args = shlex.split(hook.get('command', ''))
                if '--policy' in args and args[args.index('--policy') + 1:args.index('--policy') + 2] == [receipt['policy']]:
                    require((event, group) in expected, 'Managed entry changed; preserve it and review before managing')
    for event, group in expected:
        require(current.get('hooks', {}).get(event, []).count(group) <= 1, 'Duplicate managed entry; review before managing')
    if state is None:
        require(len(found) == len(expected), 'Managed entries missing; review ownership before managing')
    if state and state['state'] == 'uninstalled':
        require(action in {'status', 'uninstall'}, 'Use the validated installer after uninstall')
    if action == 'status':
        return {'state': state['state'] if state else 'installed', 'active_entries': len(found),
                'scope': receipt['scope'], 'preserved': ['source', 'project configuration', 'data', 'task history', 'manual shoukou', 'other hooks']}
    require(action in {'disable', 'enable', 'uninstall', 'recover'}, 'Unknown management action')
    if state and state.get('pending') and action not in {'recover', 'disable', 'uninstall'}:
        raise ValueError('Interrupted management transaction; run recover first')
    if action == 'recover':
        require(state and state.get('pending'), 'No pending management transaction')
        txn = state['pending']; actual = digest(before) if before is not None else None
        require(actual in {txn['before_sha'], txn['after_sha']}, 'Concurrent configuration change; preserve and review recovery')
        if txn['result'] == 'installed-awaiting-native-review':
            verify_version(receipt)
        if not apply:
            return {'state': 'planned', 'action': action}
        if actual == txn['before_sha']:
            write(cfg_path, txn['after'])
        state['state'] = txn['result']; state.pop('pending'); write(state_path, state)
        return {'state': state['state'], 'recovered': True}
    if action == 'enable':
        require(state is not None and state['state'] != 'uninstalled', 'Use the validated installer after uninstall')
        verify_version(receipt)
    after = copy.deepcopy(current)
    if action in {'disable', 'uninstall'}:
        for event, group in found:
            after['hooks'][event].remove(group)
    else:
        for event, group in expected:
            target = after.setdefault('hooks', {}).setdefault(event, [])
            if group not in target:
                target.append(copy.deepcopy(group))
    result = {'disable': 'disabled', 'enable': 'installed-awaiting-native-review', 'uninstall': 'uninstalled'}[action]
    plan = {'state': 'planned', 'action': action, 'matched_entries': len(found), 'result': result,
            'boundary': 'Only this installation automatic entries are managed. Shared scripts, manual rules, project adapters, business data and task/acceptance history are retained. No native trust is granted.'}
    if not apply:
        return plan
    require(snapshot(cfg_path) == before, 'Configuration changed concurrently; nothing overwritten')
    require((read(state_path) if state_path.exists() else None) == state, 'Management state changed concurrently')
    if after == current and state and state['state'] == result and not state.get('pending'):
        return dict(plan, state=result, changed=False)
    backup = safe(base / ('management-before-' + str(time.time_ns()) + '.json'))
    write(backup, {'original_bytes_hex': before.hex() if before is not None else None, 'previous_management': state})
    next_state = {'format': 'codex-task-hooks-management-v1', 'installation_policy': receipt['policy_sha256'],
                  'managed_groups': groups, 'state': state['state'] if state else 'installed',
                  'backup': str(backup), 'pending': {'before_sha': digest(before) if before is not None else None,
                    'after_sha': digest((json.dumps(after, ensure_ascii=True, indent=2) + '\n').encode()),
                    'after': after, 'result': result}}
    write(state_path, next_state)
    require(snapshot(cfg_path) == before, 'Configuration changed concurrently; pending transaction retained')
    write(cfg_path, after)
    next_state['state'] = result; next_state.pop('pending'); write(state_path, next_state)
    return dict(plan, state=result, changed=after != current, receipt=str(state_path))


def manage(home, action='status', apply=False):
    if not apply:
        return _manage(home, action, apply)
    base = safe(safe(home) / 'task-hooks')
    require((base / 'installation.json').is_file(), 'Installation receipt required; nothing was changed')
    fd = os.open(safe(base / 'management.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _manage(home, action, apply)
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    parser.add_argument('action', choices=['status', 'disable', 'enable', 'uninstall', 'recover'])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(manage(args.home, args.action, args.apply), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, StopIteration, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({'state': 'not-completed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
