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
import re
from pathlib import Path
import shlex
import tempfile
import time
import sys
import subprocess
import threading


def parser(add_help=True):
    cli = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    cli.add_argument('--home', default=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    cli.add_argument('action', choices=['status', 'disable', 'enable', 'uninstall', 'recover', 'review-diagnostic'])
    cli.add_argument('--apply', action='store_true')
    cli.add_argument('--repo'); cli.add_argument('--session'); cli.add_argument('--task')
    cli.add_argument('--failed'); cli.add_argument('--original'); cli.add_argument('--corrected'); cli.add_argument('--reason')
    return cli


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
    manifest = receipt.get('runtime_manifest')
    if manifest:
        manifest_path = safe(manifest['path'])
        require(digest(manifest_path.read_bytes()) == manifest['sha256'],
                'Frozen runtime manifest changed; reinstall after acceptance')
        root = manifest_path.parent
        names = {'task_hooks.py', 'task_guard.py', 'closeout_progress.py', 'readonly_query.py', 'manage_task_hooks.py'}
        content = read(manifest_path)
        expected = {str(root / name): content.get('files', {}).get(name) for name in names}
        actual = {item['path']: item['sha256'] for item in receipt['pinned_files']}
        require(manifest_path.name == 'RUNTIME.json' and content.get('format') == 'codex-task-hook-runtime-v1'
                and content.get('version') == root.name and actual == expected
                and {p.name for p in root.iterdir()} == names | {'RUNTIME.json'},
                'Frozen runtime dependency set changed; nothing restored')


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
                'activation': receipt.get('activation'), 'native_observation': receipt.get('native_observation'),
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


DIAGNOSTIC_TIMEOUT = 10
DIAGNOSTIC_CHILD = r'''
import ast,json,os,sys
from pathlib import Path
request=json.loads(sys.argv[1]); observations=[]
sites={item['line']:item for item in request['sites']}
def observe(path,value):
    if not isinstance(path,Path) or len(observations)>=2048:
        raise ValueError('Diagnostic input coverage is not bounded file metadata')
    observations.append({'path':str(path),'type':type(value).__name__,'object':isinstance(value,dict)})
class Instrument(ast.NodeTransformer):
    def visit_If(self,node):
        node=self.generic_visit(node)
        site=sites.get(node.lineno)
        if site:
            call=ast.Expr(value=ast.Call(func=ast.Name(id='__codex_diagnostic_observe',ctx=ast.Load()),
                args=[ast.Name(id=site['input'],ctx=ast.Load()),ast.Name(id=site['record'],ctx=ast.Load())],keywords=[]))
            return [ast.copy_location(call,node),node]
        return node
tree=ast.fix_missing_locations(Instrument().visit(ast.parse(request['source'])))
try:
    exec(compile(tree,'<reviewed-readonly-diagnostic>','exec'),{'__codex_diagnostic_observe':observe})
finally:
    target=Path(os.environ['CODEX_TASK_TMP_ROOT']).parent/'evidence/coverage.json'
    with target.open('x') as stream:
        json.dump({'format':'readonly-diagnostic-coverage-v1','records':observations},stream)
    target.chmod(0o600)
'''


def diagnostic_coverage(path):
    value = read(path); records = value.get('records') if isinstance(value, dict) else None
    require(isinstance(value, dict) and value.get('format') == 'readonly-diagnostic-coverage-v1'
            and isinstance(records, list) and 0 < len(records) <= 2048, 'Invalid diagnostic input coverage')
    require(all(isinstance(x, dict) and isinstance(x.get('path'), str) and type(x.get('object')) is bool
                and x.get('type') in {'dict', 'list', 'str', 'int', 'float', 'bool', 'NoneType'}
                and x['object'] == (x['type'] == 'dict') for x in records), 'Invalid diagnostic input types')
    return {'observed_objects': sum(x['object'] is True for x in records),
            'skipped_inputs': [x for x in records if x['object'] is False]}


def seal_diagnostic(entry, evidence):
    names = ['original-command.txt', 'corrected-command.txt', 'coverage.json', 'stdout.log', 'stderr.log']
    entry['files'] = {}
    for name in names:
        path = safe(evidence / name)
        if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
            entry['files'][name] = digest(path.read_bytes())
    entry['coverage'] = None
    try:
        entry['coverage'] = diagnostic_coverage(evidence / 'coverage.json')
    except (ValueError, OSError, KeyError, TypeError):
        pass


def run_diagnostic(core, repo, state, request, checkpoint):
    """Bounded diagnostic mode; reuse common resource checks, never label it a test."""
    attempts = state.setdefault('diagnostic_attempts', [])
    require(len(attempts) < 32, 'Diagnostic evidence limit reached; preserve and review existing attempts')
    cfg, _ = core.config_of(repo)
    before = core.observation(repo, cfg)
    root = core.run_temp_root(repo)
    work, evidence = root / 'work', root / 'evidence'
    work.mkdir(mode=0o700); evidence.mkdir(mode=0o700)
    entry = {'purpose': 'readonly-diagnostic-correction', 'repo': str(repo), 'task': state['task'],
             'session_id': state['session_id'], 'policy_sha256': state['policy_sha256'],
             'root': str(root), 'root_identity': [root.stat().st_dev, root.stat().st_ino],
             'work_identity': [work.stat().st_dev, work.stat().st_ino], 'runner_pid': os.getpid(),
             'pgid': None, 'status': 'running', 'failed_command': request['failed'],
             'failure_facts': copy.deepcopy(request['failure_facts']), 'failure_version': request['failure_version'],
             'corrected_sha256': digest(request['corrected'].encode()), 'kind': request['kind'], 'reason': request['reason'],
             'candidate': before['worktrees'][str(repo)]['content_fingerprint'], 'started_at': time.time()}
    attempts.append(entry)
    core.atomic(evidence / 'owner.json', entry); checkpoint()
    for name in ['original', 'corrected']:
        path = evidence / (name + '-command.txt')
        path.write_text(request[name]); path.chmod(0o400)
    environment = dict(os.environ, **{key: str(work) for key in ['CODEX_TASK_TMP_ROOT', 'TMPDIR', 'TMP', 'TEMP']})
    truncated = []; threads = []; child = None
    def drain(pipe, destination):
        with pipe, destination.open('xb') as stream:
            destination.chmod(0o600); count = 0
            while True:
                data = pipe.read(65536)
                if not data: break
                stream.write(data[:max(0, core.MAX_LOG_BYTES - count)]); count += len(data)
                if count > core.MAX_LOG_BYTES and destination.name not in truncated:
                    truncated.append(destination.name)
    try:
        payload = json.dumps({'source': request['argv'][-1], 'sites': request['sites']})
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', DIAGNOSTIC_CHILD, payload], cwd=repo,
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        entry['pgid'] = child.pid; checkpoint()
        for pipe, name in [(child.stdout, 'stdout.log'), (child.stderr, 'stderr.log')]:
            thread = threading.Thread(target=drain, args=(pipe, evidence / name), daemon=True)
            threads.append(thread); thread.start()
        try:
            code = child.wait(timeout=DIAGNOSTIC_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=5); code = 124
        for thread in threads: thread.join(timeout=2)
        after = core.observation(repo, cfg)
        violations = []
        if before != after: violations.append('Project source or configured output changed during read-only diagnosis')
        if truncated or any(t.is_alive() for t in threads): violations.append('log-coverage-unavailable')
        active = core.group_active(entry['pgid'])
        if type(entry['pgid']) is not int or entry['pgid'] <= 0 or active is not False:
            violations.append('active-or-unknown-descendants')
        if {p.name for p in root.iterdir()} != {'work', 'evidence'}: violations.append('output-outside-work-and-evidence')
        if any(work.iterdir()): violations.append('unexpected-diagnostic-work-output')
        if (type(entry['pgid']) is int and entry['pgid'] > 0 and active is False
                and not any(t.is_alive() for t in threads) and not any(work.iterdir())):
            core.remove_run_work(entry)
        entry.update(status='complete', exit_code=code, violations=violations, active_descendants=active,
                     work_reclaimed=not work.exists(), finished_at=time.time())
    except BaseException as error:
        entry.update(status='interrupted', error_type=type(error).__name__)
        if child is not None:
            if child.poll() is None: child.kill()
            child.wait(timeout=5)
            for thread in threads: thread.join(timeout=2)
            if not threads:
                child.stdout.close(); child.stderr.close()
            entry.update(exit_code=child.returncode, active_descendants=core.group_active(child.pid),
                         violations=['diagnostic-controller-interrupted'], finished_at=time.time())
            if entry['active_descendants'] is False and not any(t.is_alive() for t in threads) and not any(work.iterdir()):
                core.remove_run_work(entry)
            entry['work_reclaimed'] = not work.exists()
        seal_diagnostic(entry, evidence)
        core.atomic(evidence / 'result.json', entry)
        entry['receipt'] = {'path': str(evidence / 'result.json'), 'sha256': digest((evidence / 'result.json').read_bytes())}
        checkpoint()
        raise
    seal_diagnostic(entry, evidence)
    core.atomic(evidence / 'result.json', entry)
    entry['receipt'] = {'path': str(evidence / 'result.json'), 'sha256': digest((evidence / 'result.json').read_bytes())}
    checkpoint()
    return entry


def verify_diagnostic_resource(core, state, attempt):
    require(isinstance(attempt, dict), 'Invalid diagnostic resource record')
    root = safe(attempt['root']); work = safe(root / 'work')
    require(root.is_dir() and [root.stat().st_dev, root.stat().st_ino] == attempt['root_identity'], 'Run directory identity changed')
    require({p.name for p in root.iterdir()} <= {'work', 'evidence'}, 'Unknown diagnostic output remains')
    require(attempt['repo'] == state['repo'] and attempt['task'] == state['task']
            and attempt['session_id'] == state['session_id'], 'Diagnostic owner changed')
    require(attempt.get('status') in {'complete', 'interrupted'} and type(attempt.get('exit_code')) is int
            and type(attempt.get('pgid')) is int and attempt['pgid'] > 0
            and core.run_group_active(attempt) is False and not work.exists()
            and attempt.get('work_reclaimed') is True and core.evidence_ok(attempt.get('receipt')),
            'Diagnostic execution or cleanup is unfinished')
    evidence = safe(root / 'evidence')
    require(Path(attempt['receipt']['path']) == evidence / 'result.json', 'Unexpected diagnostic receipt path')
    sealed = read(evidence / 'result.json')
    require(core.digest(sealed) == core.digest({key: value for key, value in attempt.items() if key != 'receipt'}),
            'Diagnostic receipt facts changed')
    return evidence


def diagnostic_resources(core, state):
    pending = []
    for attempt in state.get('diagnostic_attempts', []):
        try:
            verify_diagnostic_resource(core, state, attempt)
        except (ValueError, OSError, KeyError, TypeError):
            pending.append({'object': 'diagnostic-resource:' + str(attempt.get('root', 'unknown') if isinstance(attempt, dict) else 'unknown'),
                            'version': core.digest(attempt)})
    return pending


def reviewed_diagnostics(core, repo, state, candidate):
    """Validate receipts on every audit; failures and failed attempts remain intact."""
    from readonly_query import diagnostic_correction
    accepted = set()
    for review in state.get('diagnostic_reviews', []):
        try:
            require(isinstance(review, dict) and review['repo'] == str(repo)
                    and review['session_id'] == state['session_id'] and review['task'] == state['task']
                    and review['policy_sha256'] == state['policy_sha256'] and review['candidate'] == candidate,
                    'Diagnostic review binding changed')
            facts = state.get('command_failures', {}).get(review['failed_command'])
            require(facts is not None and core.digest(facts) == review['failure_version'], 'Original failure changed')
            attempt = next(x for x in state.get('diagnostic_attempts', []) if x.get('receipt') == review['receipt'])
            evidence = verify_diagnostic_resource(core, state, attempt)
            require(attempt['repo'] == str(repo) and attempt['task'] == state['task']
                    and attempt['session_id'] == state['session_id'] and attempt['policy_sha256'] == state['policy_sha256']
                    and attempt['failed_command'] == review['failed_command']
                    and attempt['candidate'] == candidate and attempt['status'] == 'complete'
                    and attempt['exit_code'] == 0 and not attempt['violations'] and attempt['work_reclaimed']
                    and type(attempt['pgid']) is int and attempt['pgid'] > 0
                    and core.run_group_active(attempt) is False and core.evidence_ok(attempt['receipt']),
                    'Diagnostic execution receipt is not successful for this candidate')
            bound = ['repo', 'session_id', 'task', 'policy_sha256', 'candidate', 'failed_command',
                     'failure_facts', 'failure_version', 'files', 'kind', 'reason', 'corrected_sha256']
            require(core.digest({k: review[k] for k in bound}) == core.digest({k: attempt[k] for k in bound})
                    and core.digest(review['failure_facts']) == core.digest(facts),
                    'Review facts do not match the sealed execution')
            for name, sha in review['files'].items():
                require(name in {'original-command.txt', 'corrected-command.txt', 'coverage.json', 'stdout.log', 'stderr.log'},
                        'Unknown diagnostic evidence file')
                file = safe(evidence / name)
                require(file.is_file() and file.stat().st_size <= 2 * 1024 * 1024
                        and digest(file.read_bytes()) == sha, 'Diagnostic evidence changed')
            require(set(review['files']) == {'original-command.txt', 'corrected-command.txt', 'coverage.json', 'stdout.log', 'stderr.log'},
                    'Incomplete diagnostic evidence')
            original = (evidence / 'original-command.txt').read_text()
            require(core.digest(original) == review['failed_command'], 'Original command mismatch')
            corrected = (evidence / 'corrected-command.txt').read_text()
            relation = diagnostic_correction(original, corrected)
            require(relation['kind'] == review['kind'] and digest(corrected.encode()) == review['corrected_sha256'],
                    'Corrected query identity changed')
            expected = diagnostic_coverage(evidence / 'coverage.json')
            require(expected['observed_objects'] > 0 and expected['skipped_inputs'], 'No actual correction coverage')
            shown = {k: review[k] for k in ['observed_objects', 'skipped_inputs']}
            require(core.digest(attempt['coverage']) == core.digest(expected) == core.digest(shown),
                    'Diagnostic coverage summary changed')
            accepted.add(review['failed_command'])
        except (ValueError, OSError, KeyError, TypeError, StopIteration):
            continue  # Invalid evidence keeps the exact original failure blocking.
    return accepted


def review_diagnostic(core, repo, state, options, checkpoint):
    from readonly_query import diagnostic_correction
    require(options.repo == str(repo) and options.session == state['session_id'] and options.task == state['task'],
            'Review must name this exact repository, original task and native session')
    require(isinstance(options.failed, str) and re.fullmatch(r'[a-f0-9]{64}', options.failed), 'Exact failed command key required')
    facts = state.get('command_failures', {}).get(options.failed)
    require(isinstance(facts, dict) and type(facts.get('exit_code')) is int and facts['exit_code'] > 0,
            'Only a recorded completed failed diagnostic can be reviewed')
    original_facts = copy.deepcopy(facts)
    require(isinstance(options.reason, str) and 8 <= len(options.reason.strip()) <= 2048, 'Concrete correction reason required')
    commands = {}
    for name in ['original', 'corrected']:
        path = safe(getattr(options, name) or '')
        require(path.is_file() and path.stat().st_size <= 32768, 'Bounded original/corrected command file required')
        commands[name] = path.read_text()
    require(core.digest(commands['original']) == options.failed, 'Original text does not match the recorded failure')
    relation = diagnostic_correction(commands['original'], commands['corrected'])
    cfg, _ = core.config_of(repo)
    candidate = core.source_snapshot(repo, cfg)['content_fingerprint']
    plan = {'state': 'planned', 'action': 'review-diagnostic', 'failed_command': options.failed,
            'kind': relation['kind'], 'original_failure_preserved': True}
    if not options.apply: return plan
    for existing in state.get('diagnostic_reviews', []):
        if (existing.get('failed_command') == options.failed and existing.get('corrected_sha256') == digest(commands['corrected'].encode())
                and options.failed in reviewed_diagnostics(core, repo, state, candidate)):
            return dict(plan, state='reviewed', receipt=existing['receipt'], changed=False)
    entry = run_diagnostic(core, repo, state, dict(commands, failed=options.failed, failure_facts=original_facts,
        failure_version=core.digest(original_facts), reason=options.reason.strip(), **relation), checkpoint)
    require(entry.get('exit_code') == 0 and not entry.get('violations') and entry.get('work_reclaimed'),
            'Corrected diagnostic failed or left uncertain resources; original failure retained')
    require(entry['candidate'] == candidate and core.source_snapshot(repo, cfg)['content_fingerprint'] == candidate,
            'Project candidate changed during review; nothing accepted')
    require(state.get('command_failures', {}).get(options.failed) == original_facts,
            'Original failure changed during review; nothing accepted')
    evidence = Path(entry['root']) / 'evidence'
    coverage = diagnostic_coverage(evidence / 'coverage.json')
    require(coverage['observed_objects'] > 0 and coverage['skipped_inputs'],
            'Correction did not cover both valid objects and skipped non-objects')
    review = {'repo': str(repo), 'session_id': state['session_id'], 'task': state['task'],
              'policy_sha256': state['policy_sha256'], 'candidate': entry['candidate'],
              'failed_command': options.failed, 'failure_version': core.digest(original_facts), 'failure_facts': original_facts,
              'reason': options.reason.strip(), 'kind': relation['kind'], 'receipt': entry['receipt'],
              'corrected_sha256': digest(commands['corrected'].encode()),
              'files': copy.deepcopy(entry['files']),
              'observed_objects': coverage['observed_objects'], 'skipped_inputs': coverage['skipped_inputs']}
    proposed = copy.deepcopy(state)
    proposed.setdefault('diagnostic_reviews', []).append(review)
    require(options.failed in reviewed_diagnostics(core, repo, proposed, candidate), 'Review did not survive receipt verification')
    # The native handler owns the session lock and commits this mutation only
    # after returning an allow response. Rejected requests never checkpoint a review.
    state.setdefault('diagnostic_reviews', []).append(review)
    return dict(plan, state='reviewed', receipt=review['receipt'], observed_objects=review['observed_objects'],
                skipped_inputs=review['skipped_inputs'], changed=True)


def record_stop_observation(home, policy_sha256, state):
    """Record this callback's audit/allow decision, never infer host trust or completion."""
    base = safe(Path(home) / 'task-hooks'); path = safe(base / 'installation.json')
    if not path.is_file(): return
    fd = os.open(safe(base / 'management.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = read(path)
        require(receipt['policy_sha256'] == policy_sha256, 'Installation changed; audit observation not copied')
        receipt['activation'] = 'native-stop-audit-observed'
        receipt['native_observation'] = {'policy_sha256': policy_sha256, 'repo': state['repo'],
            'session_id': state['session_id'], 'task': state['task'], 'event': 'Stop', 'observed_at': time.time(),
            'candidate': state['last_audit']['candidate'], 'handler_audit_passed': True, 'handler_decision': 'allow',
            'boundary': 'This handler received Stop, passed its audit and chose allow. It does not prove host completion, other hooks passing, or grant native trust.'}
        write(path, receipt)
    finally:
        os.close(fd)


if __name__ == '__main__':
    args = parser().parse_args()
    try:
        require(args.action != 'review-diagnostic', 'Run diagnostic review through the matching trusted native hook in the original chat')
        print(json.dumps(manage(args.home, args.action, args.apply), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, StopIteration, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({'state': 'not-completed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
