#!/usr/bin/env python3
"""Shared, bounded task observations. This is not a shell sandbox or an authority service."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
REQUIREMENTS = {'commit', 'merge', 'push', 'local-main', 'cleanup', 'branches', 'tests', 'deploy'}
DEFAULT_REQUIREMENTS = ['commit', 'cleanup', 'branches']
LIMIT = 20000
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_RUNS = 64
BOUNDARY = ('Machine observations only. Ownership decisions and business acceptance need original records and human judgment. '
            'No deletion of external files, branches or worktrees is authorized. Not a sandbox: output detection covers Git-visible '
            'files, configured output roots and this run directory; arbitrary system/remote writes or escaped daemons are not certified.')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def no_links(path):
    path = Path(path)
    for node in [path, *path.parents]:
        require(not node.is_symlink(), 'Symlink path is not supported: ' + str(node))
    return path


def regular_json(path):
    path = no_links(path)
    require(path.is_file(), 'Ordinary JSON file required: ' + str(path))
    return json.loads(path.read_text())


def atomic(path, value):
    no_links(path.parent)
    require(not path.is_symlink(), 'State symlink rejected')
    temp = path.with_name(path.name + '.new-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def git(repo, *args, accepted=(0,)):
    result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(repo), *args],
                            capture_output=True, timeout=40, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
    require(result.returncode in accepted, 'Git query failed: ' + args[0] + ' (exit ' + str(result.returncode) + ')')
    return result


def value(repo, *args):
    return os.fsdecode(git(repo, *args).stdout).strip()


def relative(text):
    require(isinstance(text, str) and text and '\\' not in text and not Path(text).is_absolute()
            and all(p not in ('', '.', '..') for p in text.split('/')) and not any(c in text for c in '*?[]\n\r\x00'),
            'Literal repository-relative path required')
    require(text != '.git' and not text.startswith('.git/'), 'Git metadata cannot be an output or owned source')
    return text


def root_of(repo):
    requested = no_links(Path(repo).absolute())
    root = Path(value(requested, 'rev-parse', '--show-toplevel')).resolve()
    require(requested == root, 'Use the exact registered checkout root')
    return root


def common_of(repo):
    common = Path(value(repo, 'rev-parse', '--git-common-dir'))
    return no_links((repo / common).resolve() if not common.is_absolute() else common.resolve())


def config_of(repo):
    path = repo / '.codex/workflow.json'
    cfg = regular_json(path)
    require(isinstance(cfg, dict) and cfg.get('schema_version') == 1, 'Unsupported project workflow configuration')
    require(set(cfg) <= {'schema_version', 'main_branch', 'remote', 'output_roots', 'tests', 'deployment'}, 'Unknown configuration fields')
    require(isinstance(cfg.get('main_branch'), str), 'main_branch required')
    git(repo, 'check-ref-format', 'refs/heads/' + cfg['main_branch'])
    require(isinstance(cfg.get('remote', 'origin'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', cfg.get('remote', 'origin')), 'Invalid remote name')
    roots = cfg.get('output_roots')
    require(isinstance(roots, list) and len(set(roots)) == len(roots), 'Unique output_roots required')
    for item in roots:
        relative(item)
        no_links(repo / item)
    tests = cfg.get('tests', {})
    require(isinstance(tests, dict) and all(isinstance(k, str) and k and argv_valid(v) for k, v in tests.items()), 'Tests must be named argv arrays')
    deployment = cfg.get('deployment', {})
    require(isinstance(deployment, dict) and set(deployment) <= {'url', 'command'}, 'Invalid deployment fields')
    if deployment:
        require(isinstance(deployment.get('url'), str) and deployment['url'].startswith(('https://', 'http://')), 'Explicit deployment URL required')
        url = urlsplit(deployment['url'])
        require(url.hostname and not url.username and not url.password and not url.query and not url.fragment, 'Deployment target must not contain credentials, query secrets or fragments')
        require('command' not in deployment or argv_valid(deployment['command']), 'Deployment command must be argv')
    return cfg, hashlib.sha256(path.read_bytes()).hexdigest()


def argv_valid(argv):
    return isinstance(argv, list) and bool(argv) and all(isinstance(x, str) and x and '\x00' not in x for x in argv)


def worktrees(repo):
    items, item = [], {}
    for field in git(repo, 'worktree', 'list', '--porcelain', '-z').stdout.split(b'\0'):
        if not field:
            if item:
                items.append(item)
                item = {}
        else:
            key, _, val = os.fsdecode(field).partition(' ')
            item[key] = val or True
    if item:
        items.append(item)
    return items


def signature(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'type': 'missing'}
    if stat.S_ISREG(info.st_mode):
        with path.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'type': 'file', 'sha256': sha, 'mode': '100755' if info.st_mode & 0o111 else '100644', 'bytes': info.st_size}
    if stat.S_ISLNK(info.st_mode):
        return {'type': 'symlink', 'target': os.readlink(path)}
    return {'type': 'special', 'mode': info.st_mode}


def source_snapshot(repo, cfg):
    entries = {}
    index = {}
    for row in git(repo, 'ls-files', '--stage', '-z').stdout.split(b'\0'):
        if row:
            metadata, name = row.split(b'\t', 1)
            index[os.fsdecode(name)] = metadata.decode()
    raw = git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout.split(b'\0')
    changes, i = {}, 0
    while i < len(raw):
        row = raw[i]
        i += 1
        if not row:
            continue
        status_text, name = os.fsdecode(row[:2]), os.fsdecode(row[3:])
        changes[name] = {'status': status_text, 'working': signature(repo / name), 'index': index.get(name)}
        if any(x in status_text for x in 'RC') and i < len(raw):
            old = os.fsdecode(raw[i]); i += 1
            changes[old] = {'status': 'renamed-from', 'working': signature(repo / old), 'index': index.get(old)}
    names = set(index)
    names.update(os.fsdecode(p) for p in git(repo, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split(b'\0') if p)
    for name in sorted(names):
        if not any(name == output or name.startswith(output + '/') for output in cfg['output_roots']):
            entries[name] = signature(repo / name)
    hidden = [os.fsdecode(p) for p in git(repo, 'ls-files', '-v', '-z').stdout.split(b'\0') if p and (p[:1].islower() or p[:1] == b'S')]
    return {'head': value(repo, 'rev-parse', 'HEAD'), 'branch': value(repo, 'branch', '--show-current'),
            'changes': changes, 'content_fingerprint': digest(entries), 'hidden_index': hidden}


def output_snapshot(repo, cfg):
    found, uncovered = {}, []
    for output in cfg['output_roots']:
        root = repo / output
        no_links(root)
        if not root.exists():
            continue
        require(root.is_dir(), 'Output root must be a directory')
        count = 0
        for directory, directories, files in os.walk(root, followlinks=False):
            for name in list(directories):
                path = Path(directory) / name
                if path.is_symlink():
                    files.append(name); directories.remove(name)
            for name in sorted(files):
                count += 1
                if count > LIMIT:
                    uncovered.append(output)
                    directories[:] = []
                    break
                path = Path(directory) / name
                found[path.relative_to(repo).as_posix()] = signature(path)
            if count > LIMIT:
                break
    return {'files': found, 'uncovered': uncovered}


def observation(repo, cfg):
    trees = {}
    outputs = {'files': {}, 'uncovered': [], 'scope': 'all-registered-worktrees'}
    for entry in worktrees(repo):
        path = Path(entry['worktree'])
        tree = str(path)
        trees[tree] = source_snapshot(path, cfg) if path.is_dir() else {'unavailable': True, 'registration': entry}
        if trees[tree].get('unavailable'):
            outputs['uncovered'].extend(tree + ':' + root for root in cfg['output_roots'])
            continue
        observed = output_snapshot(path, cfg)
        outputs['files'].update({tree + ':' + name: sig for name, sig in observed['files'].items()})
        outputs['uncovered'].extend(tree + ':' + name for name in observed['uncovered'])
    refs = {}
    for line in value(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads').splitlines():
        name, sha = line.split(' ', 1); refs[name.removeprefix('refs/heads/')] = sha
    return {'worktrees': trees, 'branches': refs, 'outputs': outputs}


@contextlib.contextmanager
def store(repo, task, wait_seconds=0):
    require(isinstance(task, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', task) and task not in ('.', '..'), 'Explicit safe task ID required')
    require(type(wait_seconds) in (int, float) and 0 <= wait_seconds <= 5, 'Lock wait must be between zero and five seconds')
    folder = common_of(repo) / 'codex-workflow'
    no_links(folder)
    folder.mkdir(mode=0o700, exist_ok=True)
    no_links(folder / '.lock')
    fd = os.open(folder / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Task state is locked; retry without resetting it') from None
                time.sleep(min(0.05, remaining))
        yield folder, folder / (task + '.json')
    finally:
        os.close(fd)


def begin_task(repo, task, requirements=None, own_paths=(), own_branches=(), tests=()):
    repo = root_of(repo); cfg, cfg_hash = config_of(repo)
    with store(repo, task) as (_, path):
        current = observation(repo, cfg)
        if path.exists():
            state = regular_json(path)
            require(state['config_sha256'] == cfg_hash and state['repo'] == str(repo), 'Configuration/repository changed; baseline retained')
            require(requirements is None or set(requirements) == set(state['requirements']), 'Cannot reset task requirements')
        else:
            selected = requirements if requirements is not None else DEFAULT_REQUIREMENTS
            require(selected and set(selected) <= REQUIREMENTS, 'Unknown/empty requirements')
            trees = worktrees(repo)
            require(trees and not trees[0].get('bare'), 'Primary checkout cannot be identified')
            state = {'schema_version': 1, 'task': task, 'repo': str(repo), 'git_common_dir': str(common_of(repo)),
                     'config_sha256': cfg_hash, 'requirements': sorted(set(selected)), 'main_root': trees[0]['worktree'],
                     'baseline': current, 'seen_worktrees': list(current['worktrees']), 'seen_branches': dict(current['branches']),
                     'task_roots': [str(repo)], 'task_branches': [current['worktrees'][str(repo)]['branch']] if current['worktrees'][str(repo)]['branch'] not in ('', cfg['main_branch']) else [],
                     'own_paths': {str(repo): []}, 'additional_baselines': {}, 'selected_tests': [],
                     'runs': [], 'decisions': [], 'stops': {}}
        for branch in own_branches:
            require(branch in current['branches'] and branch != cfg['main_branch'], 'Owned branch must be an observed non-main branch')
            if branch not in state['task_branches']:
                require(state['baseline']['branches'].get(branch) == current['branches'][branch], 'Cannot relabel a changed protected branch')
                state['task_branches'].append(branch)
        for test in tests:
            require(test in cfg.get('tests', {}), 'Unknown selected test')
            if test not in state['selected_tests']:
                state['selected_tests'].append(test)
        for name in own_paths:
            relative(name)
            require(name in current['worktrees'][str(repo)]['changes'], '--own-path must identify an existing dirty file')
            original = state['baseline']['worktrees'][str(repo)]['changes'].get(name)
            require(name in state['own_paths'][str(repo)] or original == current['worktrees'][str(repo)]['changes'][name], 'Cannot relabel changed protected baseline content')
            if name not in state['own_paths'][str(repo)]:
                state['own_paths'][str(repo)].append(name)
        atomic(path, state)
    return {'state': 'begun', 'task': task, 'requirements': state['requirements'], 'baseline_preserved': True,
            'observed_worktrees': len(current['worktrees']), 'observed_branches': len(current['branches']), 'boundary': BOUNDARY}


def evidence_ok(evidence):
    if not isinstance(evidence, dict) or set(evidence) != {'path', 'sha256'}:
        return False
    try:
        path = no_links(Path(evidence['path']))
        return path.is_absolute() and path.is_file() and path.stat().st_size > 0 and signature(path)['sha256'] == evidence['sha256']
    except (OSError, ValueError, KeyError):
        return False


def group_active(group):
    if not group:
        return False
    p = subprocess.run(['ps', '-eo', 'pgid=,stat='], capture_output=True, text=True)
    if p.returncode:
        return None
    return any(len(parts) >= 2 and parts[0] == str(group) and not parts[1].startswith('Z')
               for parts in (line.split() for line in p.stdout.splitlines()))


def runner_active(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def reclaimed_interruption(entry):
    """Observe absent work and retained logs; never infer an old exit result."""
    try:
        root = no_links(Path(entry['root']))
        info = root.lstat()
        if (not root.is_absolute() or not root.name.startswith('codex-task-guard-')
                or not stat.S_ISDIR(info.st_mode)
                or [info.st_dev, info.st_ino] != entry['root_identity']):
            return False
        logs = [no_links(root / 'evidence' / name) for name in ['stdout.log', 'stderr.log']]
        work = root / 'work'
        return not work.exists() and not work.is_symlink() and all(path.is_file() for path in logs)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_temp_root(repo):
    # macOS's per-user TMPDIR is too long for tsx's Unix-domain socket path.
    # Use a fixed OS temporary base, never an inherited/project temp setting.
    base = no_links(Path('/tmp').resolve())
    require(base.is_dir(), 'Short system temporary base is unavailable')
    protected = [common_of(repo), *(Path(x['worktree']).resolve() for x in worktrees(repo))]
    require(not any(base.is_relative_to(root) for root in protected), 'System temporary base overlaps a project directory')
    return Path(tempfile.mkdtemp(prefix='codex-task-guard-', dir=base)).resolve()


def remove_run_work(entry):
    root = no_links(Path(entry['root']))
    require(root.is_absolute() and root.name.startswith('codex-task-guard-'), 'Recorded run directory required')
    work = no_links(root / 'work')
    require(shutil.rmtree.avoids_symlink_attacks, 'Safe directory-relative removal is unavailable')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = os.fstat(fd)
        require([identity.st_dev, identity.st_ino] == entry['root_identity'], 'Run directory identity changed')
        identity = os.stat('work', dir_fd=fd, follow_symlinks=False)
        require(stat.S_ISDIR(identity.st_mode) and [identity.st_dev, identity.st_ino] == entry['work_identity'], 'Run work directory identity changed')
        shutil.rmtree('work', dir_fd=fd)
    finally:
        os.close(fd)


def reclaim_run(repo, task, run_id, evidence_path, evidence_sha256, reason):
    repo = root_of(repo); _, cfg_hash = config_of(repo)
    require(isinstance(reason, str) and reason.strip(), 'Original resource-stop review reason required')
    evidence = {'path': str(Path(evidence_path)), 'sha256': evidence_sha256}
    require(evidence_ok(evidence), 'Original resource-stop evidence must exist and match SHA256')
    with store(repo, task) as (_, path):
        state = regular_json(path)
        require(state['repo'] == str(repo) and state['config_sha256'] == cfg_hash, 'Configuration/repository drift')
        matches = [x for x in state['runs'] if x['id'] == run_id]
        require(len(matches) == 1, 'Exact recorded run ID required')
        entry = matches[0]
        require(entry.get('status') == 'complete' and type(entry.get('exit_code')) is int,
                'Unknown/interrupted child exit cannot authorize reclamation')
        require(evidence_ok(entry.get('receipt')), 'Original run receipt must remain intact')
        require(type(entry.get('pgid')) is int and entry['pgid'] > 0, 'Recorded child process group required')
        require(group_active(entry.get('pgid')) is False, 'Run process group is active or unknown')
        work = no_links(Path(entry['root']) / 'work')
        require(not Path(evidence['path']).is_relative_to(work), 'Preserve resource-stop evidence outside reclaimed work')
        require(work.exists(), 'Run work is already absent; no deletion performed')
        remove_run_work(entry)
        record = {'run_id': run_id, 'work': str(work), 'work_identity': entry['work_identity'],
                  'evidence': evidence, 'reason': reason.strip(), 'process_group_active': False,
                  'original_receipt': entry['receipt'], 'time': time.time()}
        entry.setdefault('reclamations', []).append(record)
        entry['work_reclaimed'] = True
        atomic(path, state)
    return {'state': 'run-work-reclaimed', 'task': task, 'run_id': run_id, 'reclamation': record,
            'machine_checks_complete': False,
            'boundary': BOUNDARY + ' Reclamation records supplied evidence and process observations; the caller must read it and verify external resources have stopped. Original run failure is unchanged.'}


def add_worktree(repo, task, worktree, own_paths=()):
    repo = root_of(repo); other = root_of(worktree); cfg, cfg_hash = config_of(repo)
    require(common_of(other) == common_of(repo), 'Another repository cannot join this task')
    with store(repo, task) as (_, path):
        state = regular_json(path)
        require(state['config_sha256'] == cfg_hash, 'Configuration drift')
        require(str(other) in [x['worktree'] for x in worktrees(repo)], 'Worktree not registered')
        observed = source_snapshot(other, cfg)
        original = state['baseline']['worktrees'].get(str(other), state['additional_baselines'].get(str(other)))
        if original is None:
            state['additional_baselines'][str(other)] = observed
            original = observed
        state['own_paths'].setdefault(str(other), [])
        for name in own_paths:
            relative(name)
            require(name in observed['changes'] and original.get('changes', {}).get(name) == observed['changes'][name], 'Cannot relabel a changed protected file')
            if name not in state['own_paths'][str(other)]:
                state['own_paths'][str(other)].append(name)
        if str(other) not in state['task_roots']:
            state['task_roots'].append(str(other))
        if str(other) not in state['seen_worktrees']:
            state['seen_worktrees'].append(str(other))
        if observed['branch'] not in ('', cfg['main_branch']) and observed['branch'] not in state['task_branches']:
            state['task_branches'].append(observed['branch'])
        atomic(path, state)
    return {'state': 'worktree-added', 'task': task, 'worktree': str(other), 'baseline_preserved': True, 'boundary': BOUNDARY}


def check_task(repo, task, phase='final', decision_paths=(), stop_paths=()):
    repo = root_of(repo); cfg, cfg_hash = config_of(repo)
    require(phase in ('save', 'final'), 'Unknown phase')
    with store(repo, task) as (_, path):
        state = regular_json(path)
        require(state['repo'] == str(repo) and state['config_sha256'] == cfg_hash, 'Configuration/repository drift; baseline not reset')
        before = state['baseline']; current = observation(repo, cfg); pending = []
        reconciled = []
        baseline_trees = dict(before['worktrees'], **state.get('additional_baselines', {}))
        def finding(key, facts, allowed=()):
            pending.append({'object': key, 'version': digest([task, key, facts]), 'facts': facts, 'allowed_decisions': list(allowed)})
        for tree, snapshot in current['worktrees'].items():
            if tree not in state['seen_worktrees']:
                state['seen_worktrees'].append(tree)
            if tree not in baseline_trees:
                finding('new-worktree:' + tree, snapshot, ('other-task',))
                continue
            if snapshot.get('unavailable'):
                finding('unavailable-worktree:' + tree, snapshot)
                continue
            if snapshot['hidden_index']:
                finding('hidden-index:' + tree, snapshot['hidden_index'])
            old = baseline_trees[tree]
            if tree not in state['task_roots'] and old.get('content_fingerprint') != snapshot['content_fingerprint']:
                finding('cross-worktree-content:' + tree, {'before': old.get('content_fingerprint'), 'after': snapshot['content_fingerprint']}, ('other-task',))
            for name in sorted(set(old.get('changes', {})) | set(snapshot['changes'])):
                was, now = old.get('changes', {}).get(name), snapshot['changes'].get(name)
                owned = name in state['own_paths'].get(tree, [])
                if was is not None and not owned:
                    if now != was:
                        finding('protected-change:' + tree + ':' + name, {'before': was, 'after': now}, ('owner-update',))
                elif now is not None:
                    if tree in state['task_roots']:
                        if 'commit' in state['requirements']:
                            finding('unsaved:' + tree + ':' + name, now)
                    else:
                        finding('unattributed-change:' + tree + ':' + name, now, ('other-task',))
            if tree in state['task_roots'] and snapshot['branch'] and snapshot['branch'] != cfg['main_branch']:
                if snapshot['branch'] not in state['task_branches']:
                    state['task_branches'].append(snapshot['branch'])
        for tree in set(state['seen_worktrees']) - set(current['worktrees']):
            finding('missing-worktree:' + tree, {'previous': before['worktrees'].get(tree)})
        for name, sha in current['branches'].items():
            state['seen_branches'][name] = sha
        if phase == 'final' and 'branches' in state['requirements']:
            for name, last in state['seen_branches'].items():
                if name == cfg['main_branch']:
                    continue
                sha = current['branches'].get(name)
                owned = name in state['task_branches']
                baseline = before['branches'].get(name)
                if baseline and not owned:
                    if sha != baseline:
                        finding('changed-protected-branch:' + name, {'before': baseline, 'after': sha}, ('other-task',))
                elif sha:
                    finding('remaining-branch:' + name, {'head': sha, 'task_owned': name in state['task_branches'],
                            'checked_out_at': [p for p, s in current['worktrees'].items() if s.get('branch') == name]}, ('retained-branch', 'other-task'))
                elif not owned:
                    finding('removed-protected-branch:' + name, {'last_observed_head': last})
                elif git(repo, 'merge-base', '--is-ancestor', last, cfg['main_branch'], accepted=(0, 1)).returncode:
                    finding('removed-unmerged-task-branch:' + name, {'last_observed_head': last})
        baseline_outputs = before['outputs']
        if baseline_outputs.get('scope') != 'all-registered-worktrees':
            # Preserve pre-upgrade state honestly: its relative keys covered only the
            # initiating checkout. Never invent an initial snapshot of other roots.
            baseline_outputs = {'files': {str(repo) + ':' + name: sig for name, sig in baseline_outputs['files'].items()},
                                'uncovered': [str(repo) + ':' + name for name in baseline_outputs['uncovered']]}
            for tree in before['worktrees']:
                if tree != str(repo):
                    for root in cfg['output_roots']:
                        prefix = tree + ':' + root
                        facts = {'reason': 'Original task baseline did not observe outputs in this registered worktree',
                                 'current_files': {name: sig for name, sig in current['outputs']['files'].items()
                                                   if name == prefix or name.startswith(prefix + '/')}}
                        finding('uncovered-baseline-output-root:' + prefix, facts, ('other-task',))
        for name, sig in current['outputs']['files'].items():
            if baseline_outputs['files'].get(name) != sig:
                finding('output:' + name, sig, ('retained-output', 'other-task'))
        for name in baseline_outputs['files'].keys() - current['outputs']['files'].keys():
            finding('removed-protected-output:' + name, baseline_outputs['files'][name], ('reviewed-removal',))
        for name in sorted(set(current['outputs']['uncovered'] + baseline_outputs['uncovered'])):
            finding('uncovered-output-root:' + name, {'limit': LIMIT})
        if 'cleanup' in state['requirements'] and not cfg['output_roots']:
            finding('uncovered-output-roots', {'reason': 'No output roots configured; cleanup coverage is unavailable'})
        candidate = current['worktrees'][str(repo)]['content_fingerprint']
        failures = {}
        successes = set()
        for run in state['runs']:
            key = run['command_key']
            if run.get('status') == 'complete' and run.get('exit_code') == 0 and not run.get('violations') and evidence_ok(run.get('receipt')):
                if run.get('candidate') == candidate:
                    failures.pop(key, None); successes.add(key)
            else:
                failures[key] = run
        for key, run in failures.items():
            named = run.get('test') or key in {digest(argv) for argv in cfg.get('tests', {}).values()}
            reviewable = (not named and run.get('status') == 'complete'
                          and group_active(run.get('pgid')) is False
                          and not (Path(run['root']) / 'work').exists()
                          and not (Path(run['root']) / 'work').is_symlink()
                          and evidence_ok(run.get('receipt')))
            finding('run:' + key, {'run_id': run['id'], 'candidate': candidate, 'status': run['status'], 'exit_code': run.get('exit_code'),
                    'violations': run.get('violations'), 'group_active_now': group_active(run.get('pgid'))},
                    ('reviewed-invocation',) if reviewable else ())
        if 'cleanup' in state['requirements']:
            for position, run in enumerate(state['runs']):
                run_root = Path(run['root'])
                active = group_active(run.get('pgid'))
                replay = next((item for item in reversed(state['runs'][position + 1:])
                               if item['command_key'] == run['command_key']), {})
                if (type(run.get('pgid')) is int and run['pgid'] > 0
                        and run.get('status') in ('running', 'interrupted') and active is False
                        and runner_active(run.get('runner_pid')) is False and reclaimed_interruption(run)
                        and replay.get('status') == 'complete' and replay.get('exit_code') == 0
                        and not replay.get('violations') and replay.get('candidate') == candidate
                        and evidence_ok(replay.get('receipt'))):
                    reconciled.append({'run_id': run['id'], 'original_status': run['status'],
                                       'replayed_by': replay['id'], 'process_group_active': False,
                                       'runner_active': False, 'work_absent': True,
                                       'retained_evidence': str(run_root / 'evidence')})
                    continue
                if run.get('status') != 'complete' or active is not False:
                    finding('run-resource:' + run['id'], {'status': run['status'], 'active_group': active, 'root': str(run_root)})
                elif (run_root / 'work').exists():
                    finding('run-work:' + run['id'], {'root': str(run_root), 'status': run['status']}, ('retained-output',))
        if phase == 'final':
            head = current['worktrees'][str(repo)]['head']
            target = value(repo, 'rev-parse', cfg['main_branch'] + '^{commit}')
            if set(state['requirements']) & {'merge', 'push', 'local-main'}:
                result = git(repo, 'merge-base', '--is-ancestor', head, target, accepted=(0, 1))
                if result.returncode:
                    finding('merge', {'candidate_commit': head, 'main_commit': target})
            if 'local-main' in state['requirements']:
                main = current['worktrees'].get(state['main_root'], {})
                contains = bool(main.get('head')) and git(repo, 'merge-base', '--is-ancestor', head, main['head'], accepted=(0, 1)).returncode == 0
                if main.get('head') != target or main.get('branch') != cfg['main_branch'] or not contains:
                    finding('local-main', {'identified_original_root': state['main_root'], 'actual': main, 'target': target, 'candidate_commit': head, 'candidate_in_original_main': contains})
            if 'push' in state['requirements']:
                ref = 'refs/heads/' + cfg['main_branch']
                result = git(repo, 'ls-remote', '--exit-code', '--refs', cfg.get('remote', 'origin'), ref, accepted=(0, 2))
                if result.returncode != 0 or result.stdout.decode().split() != [target, ref]:
                    heads = [parts[0] for parts in (line.split() for line in result.stdout.decode().splitlines())
                             if len(parts) == 2 and parts[1] == ref and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', parts[0])]
                    finding('push', {'expected': target, 'query_exit': result.returncode, 'matching_target_commits': heads})
            if 'tests' in state['requirements']:
                for name in state.get('selected_tests', []):
                    argv = cfg['tests'][name]
                    if digest(argv) not in successes:
                        finding('test:' + name, {'candidate': candidate, 'command_key': digest(argv), 'executable': Path(argv[0]).name}, ('test-record',))
                if not state.get('selected_tests'):
                    finding('tests-selection', {'candidate': candidate, 'reason': 'Select relevant tests with begin --test NAME or review an exact successful ad-hoc test receipt'}, ('test-record',))
            if 'deploy' in state['requirements']:
                finding('deploy-record', {'candidate': candidate, 'url': cfg.get('deployment', {}).get('url')}, ('deployment-record',))
        for decision_path in decision_paths:
            supplied = regular_json(Path(decision_path))
            for item in supplied if isinstance(supplied, list) else [supplied]:
                require(isinstance(item, dict), 'Decision must be an object')
                require(set(item) <= {'object', 'version', 'kind', 'reason', 'evidence', 'retention', 'review_event'}, 'Unknown decision fields')
                require(all(isinstance(item.get(k), str) and item[k].strip() for k in ['object', 'version', 'kind', 'reason']), 'Exact object/version/kind/reason required')
                require(evidence_ok(item.get('evidence')), 'Original decision evidence must exist and match SHA256')
                if item['kind'] in ('reviewed-invocation', 'test-record'):
                    # This is an explicit caller review, never automatic semantic
                    # equivalence or permission to rewrite a failed run's receipt.
                    # A free-form JSON success flag or another task cannot supply proof.
                    proof = next((r for r in state['runs'] if r.get('receipt') == item['evidence']
                                  and r.get('candidate') == candidate and r.get('status') == 'complete'
                                  and r.get('exit_code') == 0 and not r.get('violations')
                                  and group_active(r.get('pgid')) is False), None)
                    require(proof is not None, 'Review requires a valid successful run of this task and current candidate')
                    if item['kind'] == 'reviewed-invocation':
                        original = failures.get(item['object'].removeprefix('run:'))
                        require(original is not None and proof.get('finished_at', 0) > original.get('finished_at', 0),
                                'Invocation review requires a later corrected run')
                if item['kind'] in ('retained-branch', 'retained-output', 'other-task'):
                    require(all(isinstance(item.get(k), str) and item[k].strip() for k in ['retention', 'review_event']), 'Retention purpose and review_event required')
                state['decisions'].append(item)
        accepted = []
        unresolved = []
        for item in pending:
            matching = [d for d in state['decisions'] if d.get('object') == item['object'] and d.get('version') == item['version']
                        and d.get('kind') in item['allowed_decisions'] and evidence_ok(d.get('evidence'))]
            if matching:
                accepted.append({'object': item['object'], 'decision': matching[-1]})
            else:
                unresolved.append(item)
        for stop_path in stop_paths:
            supplied = regular_json(Path(stop_path))
            require(isinstance(supplied, dict), 'Stops must be an object map')
            state.setdefault('stops', {}).update(supplied)
        from closeout_progress import assess
        ids = {x['object'] for x in unresolved}
        execution = assess(unresolved, {k: v for k, v in state.get('stops', {}).items() if k in ids})
        state['last_observation'] = {'candidate': candidate, 'time': time.time(), 'unfinished': execution['unfinished'], 'blocked': execution['blocked']}
        atomic(path, state)
    return {'schema_version': 1, **execution, 'phase': phase, 'completion_scope': 'saved-stage-only' if phase == 'save' else 'selected-task-final',
            'machine_checks_complete': execution['state'] == 'complete', 'task': task, 'candidate': candidate,
            'accepted_decisions': accepted, 'reconciled_run_resources': reconciled,
            'original_main': state['main_root'], 'boundary': BOUNDARY}


def run_task(repo, task, argv, test_name=None):
    repo = root_of(repo); cfg, cfg_hash = config_of(repo)
    if test_name:
        require(test_name in cfg.get('tests', {}), 'Unknown configured test')
        argv = cfg['tests'][test_name]
    require(argv_valid(argv), 'Explicit argv or --test NAME required; shell strings are not evaluated')
    with store(repo, task) as (folder, path):
        state = regular_json(path)
        require(state['config_sha256'] == cfg_hash and state['repo'] == str(repo), 'Configuration/repository drift')
        require(not any(r.get('status') == 'running'
                        and (group_active(r.get('pgid')) is not False or runner_active(r.get('runner_pid')) is not False)
                        for r in state['runs']), 'Another run is active or its exit is unknown')
        require(len(state['runs']) < MAX_RUNS, 'Run evidence limit reached; preserve failures and review/archive records explicitly')
        first = observation(repo, cfg)
        run_id = uuid.uuid4().hex
        run_root = run_temp_root(repo)
        work, evidence = run_root / 'work', run_root / 'evidence'; work.mkdir(mode=0o700); evidence.mkdir(mode=0o700)
        entry = {'id': run_id, 'status': 'running', 'candidate': first['worktrees'][str(repo)]['content_fingerprint'],
                 'command_key': digest(argv), 'executable': Path(argv[0]).name, 'argv_count': len(argv), 'test': test_name,
                 'root': str(run_root), 'root_identity': [run_root.stat().st_dev, run_root.stat().st_ino], 'runner_pid': os.getpid(), 'pgid': None}
        entry['work_identity'] = [work.stat().st_dev, work.stat().st_ino]
        state['runs'].append(entry); atomic(path, state)
    environment = dict(os.environ, **{name: str(work) for name in ['TMPDIR', 'TMP', 'TEMP', 'CODEX_TASK_TMP_ROOT']})
    try:
        truncated = []
        def drain(pipe, destination):
            with pipe, destination.open('xb') as stream:
                destination.chmod(0o600)
                count = 0
                while True:
                    data = pipe.read(65536)
                    if not data:
                        break
                    stream.write(data[:max(0, MAX_LOG_BYTES - count)])
                    count += len(data)
                    if count > MAX_LOG_BYTES and destination.name not in truncated:
                        truncated.append(destination.name)
        child = subprocess.Popen(argv, cwd=repo, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        threads = [threading.Thread(target=drain, args=(pipe, evidence / name), daemon=True)
                   for pipe, name in [(child.stdout, 'stdout.log'), (child.stderr, 'stderr.log')]]
        for thread in threads:
            thread.start()
        entry['pgid'] = child.pid
        with store(repo, task, wait_seconds=5) as (_, path):
            state = regular_json(path); state['runs'][-1] = entry; atomic(path, state)
        code = child.wait()
        for thread in threads:
            thread.join(timeout=1)
        active = group_active(child.pid)
        after = observation(repo, cfg)
        leaks = sorted(name for name, sig in after['outputs']['files'].items() if first['outputs']['files'].get(name) != sig)
        removed = sorted(first['outputs']['files'].keys() - after['outputs']['files'].keys())
        violations = ['output:' + name for name in leaks] + ['removed-output:' + name for name in removed]
        if truncated or any(t.is_alive() for t in threads):
            violations.append('log-coverage-unavailable')
        if first['worktrees'] != after['worktrees']:
            violations.append('Git-visible checkout state changed during a command restricted to task output')
        if first['outputs']['uncovered'] or after['outputs']['uncovered']:
            violations.append('output-observation-uncovered')
        if set(p.name for p in run_root.iterdir()) != {'work', 'evidence'}:
            violations.append('output-outside-work-and-evidence')
        if active is not False:
            violations.append('active-or-unknown-descendants')
        if not violations and active is False and (code == 0 or not any(work.iterdir())):
            remove_run_work(entry)
        entry.update(status='complete', exit_code=code, violations=violations, active_descendants=active,
                     work_reclaimed=not work.exists(), evidence=str(evidence), finished_at=time.time())
    except BaseException as error:
        entry.update(status='interrupted', error_type=type(error).__name__)
        with store(repo, task, wait_seconds=5) as (_, path):
            state = regular_json(path); state['runs'][-1] = entry; atomic(path, state)
        raise
    with store(repo, task, wait_seconds=5) as (_, path):
        atomic(evidence / 'result.json', entry)
        entry['receipt'] = {'path': str(evidence / 'result.json'), 'sha256': signature(evidence / 'result.json')['sha256']}
        state = regular_json(path); state['runs'][-1] = entry; atomic(path, state)
    return {'state': 'complete' if code == 0 and not violations else 'continue', 'run': entry,
            'machine_checks_complete': code == 0 and not violations, 'boundary': BOUNDARY}


def init_project(repo, main_branch=None, remote='origin', output_roots=None):
    repo = root_of(repo); path = repo / '.codex/workflow.json'
    no_links(path)
    if path.exists():
        config_of(repo)
        return {'state': 'existing-configuration-preserved', 'path': str(path), 'boundary': BOUNDARY}
    if main_branch is None:
        symbolic = git(repo, 'symbolic-ref', '--quiet', 'refs/remotes/' + remote + '/HEAD', accepted=(0, 1))
        if symbolic.returncode == 0:
            main_branch = symbolic.stdout.decode().strip().removeprefix('refs/remotes/' + remote + '/')
        else:
            for candidate in ['main', 'master']:
                if git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + candidate, accepted=(0, 1)).returncode == 0:
                    main_branch = candidate; break
    require(main_branch is not None, 'Main branch cannot be identified; specify --main-branch')
    git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + main_branch)
    roots = output_roots or []
    for name in roots:
        relative(name); no_links(repo / name)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    config = {'schema_version': 1, 'main_branch': main_branch, 'remote': remote, 'output_roots': roots, 'tests': {}}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream, indent=2); stream.write('\n')
    return {'state': 'initialized', 'path': str(path), 'deployment_target_inferred': False, 'boundary': BOUNDARY}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    init = commands.add_parser('init'); init.add_argument('--main-branch'); init.add_argument('--remote', default='origin'); init.add_argument('--output-root', action='append')
    begin = commands.add_parser('begin'); begin.add_argument('--task', required=True); begin.add_argument('--require', action='append', choices=sorted(REQUIREMENTS)); begin.add_argument('--own-path', action='append', default=[]); begin.add_argument('--own-branch', action='append', default=[]); begin.add_argument('--test', action='append', default=[])
    check = commands.add_parser('check'); check.add_argument('--task', required=True); check.add_argument('--phase', choices=['save', 'final'], default='final'); check.add_argument('--decision', action='append', default=[])
    check.add_argument('--stop', action='append', default=[])
    extra = commands.add_parser('add-worktree'); extra.add_argument('--task', required=True); extra.add_argument('--path', required=True); extra.add_argument('--own-path', action='append', default=[])
    run = commands.add_parser('run'); run.add_argument('--task', required=True); run.add_argument('--test'); run.add_argument('argv', nargs=argparse.REMAINDER)
    reclaim = commands.add_parser('reclaim-run'); reclaim.add_argument('--task', required=True); reclaim.add_argument('--run', required=True)
    reclaim.add_argument('--evidence', required=True); reclaim.add_argument('--sha256', required=True); reclaim.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'init':
            result = init_project(args.repo, args.main_branch, args.remote, args.output_root)
        elif args.action == 'begin':
            result = begin_task(args.repo, args.task, args.require, args.own_path, args.own_branch, args.test)
        elif args.action == 'check':
            result = check_task(args.repo, args.task, args.phase, args.decision, args.stop)
        elif args.action == 'add-worktree':
            result = add_worktree(args.repo, args.task, args.path, args.own_path)
        elif args.action == 'reclaim-run':
            result = reclaim_run(args.repo, args.task, args.run, args.evidence, args.sha256, args.reason)
        else:
            require(not (args.test and args.argv), 'Choose --test or argv')
            result = run_task(args.repo, args.task, args.argv[1:] if args.argv[:1] == ['--'] else args.argv, args.test)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 3 if result['state'] in ('continue', 'blocked') else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(json.dumps({'state': 'input-error', 'error': str(error), 'boundary': BOUNDARY}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
