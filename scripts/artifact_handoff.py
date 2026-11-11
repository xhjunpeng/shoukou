#!/usr/bin/env python3
"""Read-only bridge from dev-artifacts to shoukou. Never grants deletion rights.

Only reads the selected repository's small ledger and Git identity; no recursive
artifact scan, database access, shell execution, service action or file write.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

sys.dont_write_bytecode = True
import inspect_repo as git_state

MAX_BYTES = 8 * 1024 * 1024
KINDS = {'dependency', 'cache', 'build', 'test-data', 'database', 'snapshot',
         'backup', 'evidence', 'log', 'business-data', 'unknown'}
TEMPORARY = {'dependency', 'cache', 'build', 'test-data', 'log'}
STATES = {'active', 'paused', 'ready-for-closeout'}
BOUNDARY = ('Ledger declarations are hints, not authenticated ownership, delivery, '
            'backup, complete coverage, app occupancy or deletion permission. '
            'No file, Git reference, ledger or service was modified.')


def absolute(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Invalid recorded path')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        raise ValueError('Use a canonical absolute recorded path')
    return path


def no_links(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Symbolic-link ledger paths are not supported')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value):
        raise ValueError('Invalid recorded identifier')
    return value


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON keys are not accepted')
        result[key] = value
    return result


def read_ledger(path):
    no_links(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or info.st_nlink != 1:
        raise ValueError('Ledger must be one ordinary file within the size limit')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Ledger size limit exceeded')
    return json.loads(raw, object_pairs_hook=duplicate_keys), hashlib.sha256(raw).hexdigest()


def prepare(repo, task_id=None, state_dir=None):
    root = Path(git_state.value(repo, 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git_state.value(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    project_id = hashlib.sha256(os.fsencode(common)).hexdigest()[:24]
    state = Path(state_dir or (Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'dev-artifacts')).expanduser().absolute()
    no_links(state)
    if git_state.inside(state, root) or git_state.inside(state, common):
        raise ValueError('Ledger location must be outside worktrees and Git metadata')
    ledger = state / project_id / 'registry.json'
    branch = git_state.value(root, 'branch', '--show-current')
    result = git_state.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'])
    head = result.stdout.decode().strip() if result.returncode == 0 else None
    out = {'schema_version': 1, 'scope': 'selected', 'project_id': project_id,
           'worktree': str(root), 'head': head, 'branch': branch, 'ledger': str(ledger),
           'cleanup_authorized': False, 'boundary': BOUNDARY}
    data, sha = read_ledger(ledger)
    if data is None:
        return dict(out, status='unregistered', next_action='Use current task records; do not infer empty artifacts or scan other projects')
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('project_id') != project_id or data.get('git_common_dir') != str(common)
            or not isinstance(data.get('tasks'), dict)):
        raise ValueError('Ledger repository identity or schema mismatch')
    out['ledger_sha256'] = sha
    tasks = data['tasks']
    for tid, task in tasks.items():
        identifier(tid)
        if (not isinstance(task, dict) or task.get('state') not in STATES
                or not isinstance(task.get('artifacts'), dict)):
            raise ValueError('Malformed task record; preserve original ledger')
        absolute(task.get('worktree'))
        for aid, item in task['artifacts'].items():
            identifier(aid)
            if not isinstance(item, dict) or item.get('kind') not in KINDS:
                raise ValueError('Malformed artifact record')
            absolute(item.get('path'))
            if type(item.get('shared')) is not bool or type(item.get('external')) is not bool:
                raise ValueError('Malformed resource ownership flags')
    if task_id is None:
        candidates = []
        for tid, task in tasks.items():
            obs = task.get('last_observation') or {}
            if not isinstance(obs, dict):
                raise ValueError('Malformed task observation')
            recorded_branch = obs.get('branch', task.get('initial_branch'))
            if task['worktree'] == str(root) and task['state'] != 'paused' and recorded_branch == branch:
                candidates.append(tid)
        if len(candidates) != 1:
            return dict(out, status='needs-task-selection', candidates=candidates,
                        next_action='Resolve the task from current conversation and task records; never pick the newest task by time')
        task_id = candidates[0]
    task_id = identifier(task_id)
    task = tasks.get(task_id)
    if task is None or task['worktree'] != str(root):
        raise ValueError('Task is absent or belongs to a different worktree')
    out.update(task=task_id, task_state=task['state'])
    if task['state'] == 'paused':
        return dict(out, status='protected-paused', next_action='Preserve paused task; do not resume it through closeout')
    obs = task.get('last_observation')
    if not isinstance(obs, dict):
        return dict(out, status='needs-refresh', next_action='Run dev-artifacts handoff for this exact task before closeout')
    issues = []
    try:
        stamp = datetime.fromisoformat(obs['observed_at'])
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if not -5 <= age <= 120:
            issues.append('observation-stale-or-future')
    except (KeyError, ValueError, TypeError):
        issues.append('observation-time-invalid')
    if obs.get('head') != head or obs.get('branch') != branch:
        issues.append('observation-version-mismatch')
    if task['state'] != 'ready-for-closeout':
        issues.append('task-handoff-not-prepared')
    observations = obs.get('artifacts')
    if not isinstance(observations, list) or any(not isinstance(row, dict) for row in observations):
        raise ValueError('Malformed artifact observations')
    by_id = {row.get('artifact'): row for row in observations}
    if len(by_id) != len(observations) or set(by_id) != set(task['artifacts']):
        raise ValueError('Observation does not cover the exact registered artifact set')
    rows = []
    for aid, item in task['artifacts'].items():
        path = absolute(item['path'])
        if (git_state.inside(root, path) or git_state.inside(path, common)
                or git_state.inside(path, state) or '.git' in path.parts
                or path == Path.home() or path == Path(path.anchor)):
            raise ValueError('Broad roots, Git data and ledger paths cannot be artifact targets')
        if not git_state.inside(path, root) and not item['external']:
            raise ValueError('Outside artifact lacks its external-resource marker')
        row = by_id[aid]
        if row.get('path') != str(path) or row.get('kind') != item['kind'] or row.get('origin') != item.get('origin'):
            raise ValueError('Artifact observation identity mismatch')
        flags = row.get('review_flags')
        measurement = row.get('measurement')
        if (not isinstance(flags, list) or any(not isinstance(x, str) for x in flags)
                or not isinstance(measurement, dict)):
            raise ValueError('Malformed artifact review flags or measurement')
        flags = set(flags)
        if item.get('origin') != 'planned-absent': flags.add('preexisting-or-origin-unproven')
        if item['kind'] not in TEMPORARY: flags.add('protected-resource-kind')
        if item['shared'] or item['external']: flags.add('shared-or-external-reference')
        if path.name in {'.runtime', '.gaga', 'data'}: flags.add('mixed-root-must-be-split')
        if path.name.startswith('.env') or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.pem', '.key'}:
            flags.add('potential-persistent-or-sensitive-data')
        if any(p.is_symlink() for p in (path, *path.parents)): flags.add('symbolic-link-path-needs-review')
        if not item.get('rebuild'): flags.add('rebuild-not-documented')
        if not measurement.get('complete'): flags.add('measurement-incomplete')
        for other_id, other in tasks.items():
            if other_id != task_id and any(git_state.inside(path, absolute(a['path'])) or git_state.inside(absolute(a['path']), path) for a in other['artifacts'].values()):
                flags.add('overlapping-task-reference:' + other_id)
        hard_flags = flags - {'usage-and-restart-dependencies-not-verified'}
        route = 'preserve-and-investigate' if issues or hard_flags else 'review-for-reclamation'
        if measurement.get('state') == 'absent': route = 'absent-not-proof-of-our-cleanup'
        rows.append({'artifact': aid, 'path': str(path), 'kind': item['kind'], 'route': route,
                     'review_flags': sorted(flags), 'cleanup_authorized': False})
    candidates, gaps = obs.get('unregistered_candidates'), obs.get('discovery_gaps')
    if not isinstance(candidates, list) or not isinstance(gaps, list):
        raise ValueError('Missing bounded-discovery results')
    if read_ledger(ledger)[1] != sha:
        raise ValueError('Ledger changed during handoff; reread without overwriting it')
    if git_state.value(root, 'branch', '--show-current') != branch:
        raise ValueError('Branch changed during handoff')
    current = git_state.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'])
    if (current.stdout.decode().strip() if current.returncode == 0 else None) != head:
        raise ValueError('HEAD changed during handoff')
    return dict(out, status='needs-refresh' if issues else 'ready-for-review', issues=issues,
                artifacts=rows, unregistered_candidates=candidates, discovery_gaps=gaps,
                coverage='Registered outputs plus bounded discovery; not a full project audit',
                next_action='Verify scope, acceptance, current dependencies, unique content and existing shoukou checks before any disposal')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--task')
    parser.add_argument('--state-dir')
    args = parser.parse_args()
    try:
        result = prepare(args.repo, args.task, args.state_dir)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result['status'] == 'ready-for-review' else 3
    except (ValueError, OSError, KeyError, TypeError, git_state.subprocess.TimeoutExpired) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error), 'cleanup_authorized': False, 'boundary': BOUNDARY}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
