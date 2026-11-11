#!/usr/bin/env python3
"""Read-only Git inventory and fail-closed worktree cleanup preflight."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ['GIT_OPTIONAL_LOCKS'] = '0'


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, timeout=60)


def git(repo, *args):
    r = run(['git', '-C', str(repo), *args])
    if r.returncode:
        raise ValueError(f'Git query failed: {args[0]} (exit {r.returncode})')
    return r.stdout


def value(repo, *args):
    return git(repo, *args).decode('utf-8', 'surrogateescape').strip()


def ancestor(repo, head, base):
    r = run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', head, base])
    if r.returncode not in (0, 1):
        raise ValueError('Cannot establish commit ancestry')
    return r.returncode == 0


def paths(repo, *args):
    return [os.fsdecode(p) for p in git(repo, *args).split(b'\0') if p]


def worktrees(repo):
    result = []
    item = {}
    for field in git(repo, 'worktree', 'list', '--porcelain', '-z').split(b'\0'):
        if not field:
            if item:
                result.append(item)
                item = {}
            continue
        key, _, val = os.fsdecode(field).partition(' ')
        item[key] = val if val else True
    if item:
        result.append(item)
    return result


def snapshot(path):
    root = Path(value(path, 'rev-parse', '--show-toplevel')).resolve()
    if root != Path(path).resolve():
        raise ValueError('Target must be the exact worktree root')
    head = value(root, 'rev-parse', 'HEAD')
    branch = value(root, 'branch', '--show-current')
    changes = {
        'staged': paths(root, 'diff', '--cached', '--name-only', '-z'),
        'unstaged': paths(root, 'diff', '--name-only', '-z'),
        'untracked': paths(root, 'ls-files', '--others', '--exclude-standard', '-z')
    }
    status = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    ignored = paths(root, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z')
    metadata = []
    nested = []
    usage = {'logical_bytes': 0, 'allocated_bytes': 0, 'file_count': len(ignored), 'groups': []}
    groups, inodes = {}, set()
    for name in ignored:
        p = root / name
        s = p.lstat()
        metadata.append([name, s.st_mode, s.st_size, s.st_mtime_ns])
        # lstat counts the link itself, never the data reached through it.
        group = '/'.join(Path(name).parts[:2])
        item = groups.setdefault(group, {'path': group, 'logical_bytes': 0, 'allocated_bytes': 0, 'file_count': 0})
        inode = (s.st_dev, s.st_ino)
        allocated = s.st_blocks * 512 if inode not in inodes else 0
        inodes.add(inode)
        for entry in (usage, item):
            entry['logical_bytes'] += s.st_size
            entry['allocated_bytes'] += allocated
        item['file_count'] += 1
    usage['groups'] = sorted(groups.values(), key=lambda item: (-item['allocated_bytes'], item['path']))
    filesystem = os.statvfs(root)
    # Nested .git markers can be hidden by ignore rules; don't follow symlinks.
    for directory, dirs, files in os.walk(root, followlinks=False):
        if directory == str(root):
            dirs[:] = [d for d in dirs if d != '.git']
            files = [f for f in files if f != '.git']
        if '.git' in dirs or '.git' in files:
            nested.append(str(Path(directory).relative_to(root)))
            dirs[:] = []
    digest = hashlib.sha256()
    digest.update(head.encode())
    digest.update(branch.encode())
    digest.update(status)
    digest.update(json.dumps(metadata, ensure_ascii=True).encode())
    digest.update(json.dumps(sorted(nested)).encode())
    return {'path': str(root), 'head': head,
            'branch': branch,
            'clean': not status, 'changes': changes, 'status': os.fsdecode(status).replace('\0', '\n'),
            'ignored': ignored, 'ignored_groups': paths(root, 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory', '-z'), 'nested_repositories': sorted(nested),
            'ignored_metadata': metadata,
            'ignored_usage': usage,
            'filesystem': {'device': root.stat().st_dev, 'available_bytes': filesystem.f_bavail * filesystem.f_frsize},
            'submodules': (root / '.gitmodules').exists(),
            'fingerprint': digest.hexdigest()}


def commit_in_base(repo, head, base):
    if run(['git', '-C', str(repo), 'cat-file', '-e', head + '^{commit}']).returncode:
        return False
    return ancestor(repo, head, base)


def merged_proof(repo, branch, head, base_branch, base_head, prs):
    if ancestor(repo, head, base_head):
        return 'committed-head-in-base'
    for p in prs:
        merge = (p.get('mergeCommit') or {}).get('oid')
        if (p.get('state') == 'MERGED' and p.get('headRefName') == branch
                and p.get('baseRefName') == base_branch and p.get('headRefOid') == head
                and merge and commit_in_base(repo, merge, base_head)):
            return 'exact-head-merged-via-pr'
    return 'not-proven-in-base'


def github(repo, offline, base_branch):
    if offline:
        return {'status': 'not-queried', 'prs': []}
    r = run(['git', '-C', str(repo), 'remote', 'get-url', 'origin'])
    url = os.fsdecode(r.stdout).strip()
    import re
    match = re.fullmatch(r'(?:https://github.com/|git@github.com:|ssh://git@github.com/)([^/]+/[^/]+?)(?:\.git)?', url)
    if not match:
        return {'status': 'not-github', 'prs': []}
    repository = match.group(1)
    try:
        r = run(['gh', 'pr', 'list', '--repo', repository, '--state', 'all', '--limit', '100',
                 '--json', 'number,state,headRefName,headRefOid,baseRefName,mergeCommit,mergedAt,url'])
        if r.returncode:
            return {'status': 'unavailable', 'prs': []}
        prs = json.loads(r.stdout)
        if not isinstance(prs, list):
            raise ValueError('Invalid PR response')
        checks = {}
        for key, endpoint in [('actions_enabled', f'repos/{repository}/actions/permissions'),
                              ('branch_protection', f'repos/{repository}/branches/{base_branch}')]:
            response = run(['gh', 'api', endpoint])
            try:
                data = json.loads(response.stdout) if response.returncode == 0 else {}
                checks[key] = data.get('enabled' if key == 'actions_enabled' else 'protected', 'unavailable')
            except ValueError:
                checks[key] = 'unavailable'
        return {'status': 'queried-limited-to-100', 'prs': prs, **checks}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {'status': 'unavailable', 'prs': []}


def resolve_base(repo, requested):
    if requested:
        ref = requested
    else:
        r = run(['git', '-C', str(repo), 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'])
        ref = os.fsdecode(r.stdout).strip()
        if not ref:
            for candidate in ('origin/main', 'main', 'origin/master', 'master'):
                if run(['git', '-C', str(repo), 'rev-parse', '--verify', candidate + '^{commit}']).returncode == 0:
                    ref = candidate
                    break
    if not ref or ref.startswith('-'):
        raise ValueError('Target base is unknown')
    return ref, value(repo, 'rev-parse', '--verify', ref + '^{commit}')


def policy_marker(repo, ref):
    r = run(['git', '-C', str(repo), 'show', ref + ':AGENTS.md'])
    import re
    match = re.search(r'closeout-policy: ([^ >\n]+)', os.fsdecode(r.stdout))
    return match.group(1) if match else 'unversioned'


def inventory(repo, base, offline, target=None):
    repo = Path(value(repo, 'rev-parse', '--show-toplevel')).resolve()
    ref, base_head = resolve_base(repo, base)
    gh = github(repo, offline, ref.removeprefix('origin/'))
    trees = worktrees(repo)
    if target is not None:
        chosen = Path(target)
        if (not chosen.is_absolute() or chosen.is_symlink()
                or not any(Path(t['worktree']).resolve() == chosen.resolve() for t in trees)):
            raise ValueError('Selected path must be an exact registered worktree root')
    reports = []
    for t in trees:
        if target is not None and Path(t['worktree']).resolve() != Path(target).resolve():
            continue
        try:
            s = snapshot(t['worktree'])
            s['committed_merge'] = merged_proof(repo, s['branch'], s['head'], ref.removeprefix('origin/'), base_head, gh['prs'])
            s['delivery'] = 'uncommitted-work-remains' if not s['clean'] else 'task-acceptance-not-assessed'
            s['cleanup'] = 'not-assessed'
            reports.append(s)
        except (ValueError, OSError, subprocess.TimeoutExpired) as e:
            reports.append({'path': t['worktree'], 'error': str(e), 'cleanup': 'blocked'})
    branches = []
    for line in value(repo, 'for-each-ref', '--format=%(refname:short)|%(objectname)', 'refs/heads').splitlines():
        name, head = line.rsplit('|', 1)
        policy = policy_marker(repo, name)
        branches.append({'branch': name, 'head': head, 'policy_version': policy,
                         'committed_merge': merged_proof(repo, name, head, ref.removeprefix('origin/'), base_head, gh['prs'])})
    common = Path(value(repo, 'rev-parse', '--git-common-dir'))
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    return {'schema_version': 1, 'repository': str(repo), 'git_common_dir': str(common),
            'observed_at_unix': time.time(), 'base_ref': ref, 'base_head': base_head,
            'base_freshness': 'local-reference-only-no-fetch', 'policy_ref': ref, 'policy_version': policy_marker(repo, ref), 'github': gh,
            'worktrees': reports, 'branches': branches,
            'notice': 'Commit inclusion is not task delivery or permission to delete. Inventory never authorizes cleanup.'}


def summary(report):
    """Bound display volume while retaining every candidate and dirty-file list."""
    result = dict(report)
    result['worktrees'] = []
    for state in report['worktrees']:
        item = {k: v for k, v in state.items() if k not in ('ignored', 'ignored_groups', 'ignored_metadata')}
        item['ignored_count'] = len(state.get('ignored', []))
        groups = state.get('ignored_groups', [])
        item['ignored_groups_count'] = len(groups)
        item['ignored_groups_sample'] = groups[:12]
        if 'ignored_usage' in item:
            usage = item['ignored_usage']
            item['ignored_usage'] = {k: v for k, v in usage.items() if k != 'groups'}
            item['ignored_usage']['groups_count'] = len(usage['groups'])
            item['ignored_usage']['groups_sample'] = usage['groups'][:12]
        result['worktrees'].append(item)
    result['github'] = {k: v for k, v in report['github'].items() if k != 'prs'}
    result['github']['pr_count'] = len(report['github']['prs'])
    result['notice'] += ' Summary omits ignored-file and PR details; use full inventory for cleanup evidence.'
    return result


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def inside(child, parent):
    return child == parent or parent in child.parents


def classify_ignored(names, dispositions, directory_actions=('discard',)):
    if not isinstance(dispositions, dict):
        return None
    keys = set(dispositions)
    if any(not isinstance(key, str) or Path(key).is_absolute() or '..' in Path(key).parts for key in keys):
        return None
    expanded = {}
    matched = set()
    for name in names:
        # Only an exact key or a directory ancestor can match this path.
        # Cost depends on path depth, not the total number of dispositions.
        candidates = {name}
        candidates.update(name[:i + 1] for i, char in enumerate(name) if char == '/')
        rules = candidates & keys
        if len(rules) != 1:
            return None
        rule = next(iter(rules))
        if rule.endswith('/') and (not isinstance(dispositions[rule], dict) or dispositions[rule].get('action') not in directory_actions):
            return None
        matched.add(rule)
        expanded[name] = dispositions[rule]
    return expanded if matched == keys else None


def app_check(app, target):
    """Validate observations; native shared checks stay delegated to the app."""
    blockers = []
    native = None
    if (not isinstance(app, dict) or not isinstance(app.get('source'), str)
            or not app.get('source') or not isinstance(app.get('chats'), list)):
        return ['app-occupancy-query-incomplete'], None
    mode = app.get('mode', 'complete-query')
    if mode not in ('complete-query', 'native-archive'):
        blockers.append('unsupported-app-check-mode')
    if mode == 'complete-query' and app.get('complete') is not True:
        blockers.append('app-occupancy-query-incomplete')
    valid_chats = []
    for chat in app['chats']:
        if (not isinstance(chat, dict) or not chat.get('id') or type(chat.get('archived')) is not bool
                or not isinstance(chat.get('cwd'), str) or not Path(chat['cwd']).is_absolute()):
            blockers.append('chat-observation-incomplete')
            continue
        valid_chats.append(chat)
        cwd = Path(chat['cwd']).resolve()
        if not chat['archived'] and (inside(cwd, target) or not cwd.is_dir()):
            blockers.append('continuing-chat-has-target-or-missing-directory')
    if mode == 'native-archive':
        archive = app.get('archive')
        if not isinstance(archive, dict):
            blockers.append('native-archive-binding-missing')
        else:
            owner = archive.get('owner_thread_id')
            if (archive.get('tool') != 'mcp__codex_app__archive_worktree'
                    or not isinstance(archive.get('identity_key'), str) or not archive.get('identity_key')
                    or not isinstance(archive.get('contract_source'), str) or not archive.get('contract_source')
                    or not isinstance(archive.get('attachment_source'), str) or not archive.get('attachment_source')
                    or not isinstance(archive.get('target_path'), str) or not Path(archive['target_path']).is_absolute()
                    or Path(archive['target_path']).resolve() != target
                    or not isinstance(owner, str) or not owner or archive.get('current_thread_id') != owner
                    or not any(c['id'] == owner and not c['archived'] for c in valid_chats)):
                blockers.append('native-archive-binding-invalid')
            else:
                native = {'tool': archive['tool'], 'root': archive['identity_key'], 'owner_thread_id': owner}
    return blockers, native


def cleanup_check(repo, report, target, evidence_path):
    blockers = []
    target = Path(target).resolve()
    trees = worktrees(repo)
    entry = next((t for t in trees if Path(t['worktree']).resolve() == target), None)
    if not entry:
        raise ValueError('Target is not a registered worktree')
    if target == Path(trees[0]['worktree']).resolve():
        blockers.append('primary-checkout-must-be-retained')
    if entry.get('locked') or entry.get('prunable'):
        blockers.append('worktree-registration-locked-or-prunable')
    state = snapshot(target)
    if not state['clean']:
        blockers.append('staged-unstaged-or-untracked-work-remains')
    if state['nested_repositories'] or state['submodules']:
        blockers.append('nested-repository-or-submodule-needs-separate-preservation')
    proof = merged_proof(repo, state['branch'], state['head'], report['base_ref'].removeprefix('origin/'), report['base_head'], report['github']['prs'])
    if proof == 'not-proven-in-base':
        blockers.append('current-committed-head-not-proven-merged')
    epath = Path(evidence_path).resolve()
    if inside(epath, target):
        raise ValueError('Evidence must live outside the cleanup target')
    evidence = json.loads(epath.read_text())
    if evidence.get('schema_version') != 1:
        blockers.append('unsupported-evidence-schema')
    observed = evidence.get('observed_at_unix')
    if not isinstance(observed, (int, float)) or not 0 <= time.time() - observed <= 120:
        blockers.append('evidence-missing-stale-or-future')
    expected = {'path': str(target), 'head': state['head'], 'fingerprint': state['fingerprint'], 'branch': state['branch'],
                'base_ref': report['base_ref'], 'base_head': report['base_head']}
    if evidence.get('target') != expected:
        blockers.append('target-or-base-changed-since-evidence')
    if evidence.get('cleanup_authorized') is not True:
        blockers.append('cleanup-not-authorized')
    # These observations must come from the supported app/process queries.
    # The script checks consistency; it cannot authenticate an agent-written claim.
    app_blockers, native = app_check(evidence.get('app', {}), target)
    blockers.extend(app_blockers)
    proc = evidence.get('processes', {})
    if not isinstance(proc, dict) or proc.get('complete') is not True or proc.get('active') != [] or not proc.get('source'):
        blockers.append('process-query-incomplete-or-target-in-use')
    if evidence.get('task_delivery') != {'accepted': True, 'head': state['head']}:
        blockers.append('task-acceptance-not-bound-to-current-head')
    dispositions = classify_ignored(state['ignored'], evidence.get('ignored'))
    if dispositions is None:
        blockers.append('ignored-files-not-fully-classified')
    else:
        for name, d in dispositions.items():
            if not isinstance(d, dict) or not d.get('reason'):
                blockers.append('ignored-file-disposition-invalid')
                continue
            if d.get('action') == 'discard':
                continue
            if d.get('action') != 'preserve' or not isinstance(d.get('backup'), str):
                blockers.append('ignored-file-disposition-invalid')
                continue
            src = target / name
            backup_path = Path(d['backup'])
            backup = backup_path.resolve()
            if (not backup_path.is_absolute() or src.is_symlink() or backup_path.is_symlink() or inside(backup, target)
                    or not src.is_file() or not backup.is_file()):
                blockers.append('backup-unusable-or-inside-target')
                continue
            digest = file_digest(src)
            if d.get('sha256') != digest or file_digest(backup) != digest:
                blockers.append('backup-content-mismatch')
    # Detect changes during potentially slow backup verification too.
    if snapshot(target)['fingerprint'] != state['fingerprint'] or value(repo, 'rev-parse', report['base_ref'] + '^{commit}') != report['base_head']:
        blockers.append('target-or-base-changed-during-check')
    return {'schema_version': 1, 'target': expected, 'committed_merge': proof,
            'cleanup_preflight': 'blocked' if blockers else ('native-archive-ready' if native else 'passed'),
            'required_native_action': native,
            'blockers': sorted(set(blockers)),
            'boundary': 'Read-only check; app/process observations supplied by caller. No deletion executed; rerun immediately before the supported cleanup action.'}


def daily_inventory(repo, base=None):
    """Current checkout only. Never a deletion preflight or full inventory."""
    requested = Path(repo)
    root = Path(value(requested, 'rev-parse', '--show-toplevel')).resolve()
    if requested.is_symlink() or requested.resolve() != root:
        raise ValueError('Daily inspection requires the exact checkout root')
    head = value(root, 'rev-parse', 'HEAD')
    ref, base_head = resolve_base(root, base)
    status = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal')
    return {
        'schema_version': 1,
        'mode': 'daily-scoped-v2',
        'repository': str(root),
        'head': head,
        'branch': value(root, 'branch', '--show-current'),
        'base_ref': ref,
        'base_head': base_head,
        'base_freshness': 'local-reference-only-no-fetch',
        'committed_head_in_local_base': ancestor(root, head, base_head),
        'status': os.fsdecode(status).replace('\0', '\n'),
        'tracked_and_untracked_clean': not status,
        'staged': paths(root, 'diff', '--cached', '--name-only', '-z'),
        'unstaged': paths(root, 'diff', '--name-only', '-z'),
        'ignored_files': 'not-scanned',
        'untracked_directories': 'collapsed-by-git; inspect task-owned contents before commit',
        'other_worktrees': 'not-scanned',
        'remote': 'not-queried',
        'delivery': 'not-assessed',
        'cleanup_authorized': False,
        'notice': 'Read-only daily observation. Not proof of acceptance, remote delivery, resource cleanup or whole-directory preservation.',
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', default='.')
    p.add_argument('--daily', action='store_true', help='Current checkout Git status only; no ignored-file scan, network or cleanup preflight')
    p.add_argument('--base')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--json', action='store_true')
    p.add_argument('--summary', action='store_true', help='Bound inventory output; not a cleanup evidence source')
    p.add_argument('--worktree', metavar='WORKTREE', help='Inventory only this registered worktree; not cleanup authorization')
    p.add_argument('--artifacts', action='store_true', help='Read dev-artifacts handoff for the selected worktree')
    p.add_argument('--artifact-task', help='Exact task ID from current task context')
    p.add_argument('--check-cleanup', metavar='WORKTREE')
    p.add_argument('--evidence', metavar='JSON')
    args = p.parse_args()
    try:
        if args.daily:
            if any((args.check_cleanup, args.evidence, args.worktree, args.artifacts, args.artifact_task, args.summary)):
                raise ValueError('--daily cannot supply whole-directory or artifact cleanup evidence')
            print(json.dumps(daily_inventory(args.repo, args.base), ensure_ascii=True, indent=2))
            return 0
        if bool(args.check_cleanup) != bool(args.evidence):
            raise ValueError('--check-cleanup and --evidence must be supplied together')
        if args.summary and args.check_cleanup:
            raise ValueError('--summary is inventory-only')
        if args.worktree and args.check_cleanup:
            raise ValueError('--worktree and --check-cleanup are mutually exclusive')
        if (args.artifact_task and not args.artifacts) or (args.artifacts and (not args.worktree or args.check_cleanup)):
            raise ValueError('Artifact handoff requires --artifacts with --worktree; it is not a cleanup preflight')
        report = inventory(args.repo, args.base, args.offline, target=args.check_cleanup or args.worktree)
        if args.worktree:
            report['inventory_scope'] = 'selected-worktree'
            report['selected_worktree'] = str(Path(args.worktree).resolve())
        if args.artifacts:
            import artifact_handoff
            report['artifact_handoff'] = artifact_handoff.prepare(args.worktree, args.artifact_task)
        if args.check_cleanup:
            report = cleanup_check(report['repository'], report, args.check_cleanup, args.evidence)
        elif args.summary:
            report = summary(report)
        if args.json or args.check_cleanup:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            print(f"repository={report['repository']}\nbase={report['base_ref']}\nbase_head={report['base_head']}\nbase_freshness={report['base_freshness']}")
            print('\n[worktree-status]')
            for s in report['worktrees']:
                print(json.dumps({k: v for k, v in s.items() if k not in ('ignored', 'ignored_metadata')}, ensure_ascii=True))
                print('ignored_files=' + str(s.get('ignored_count', len(s.get('ignored', [])))))
            print('\n[local-branches]')
            for b in report['branches']:
                print(json.dumps(b, ensure_ascii=True))
            print('\n[github]\n' + json.dumps(report['github'], ensure_ascii=True))
            print(report['notice'])
            if 'artifact_handoff' in report:
                print('\n[artifact-handoff]\n' + json.dumps(report['artifact_handoff'], ensure_ascii=True))
        if args.artifacts and report['artifact_handoff']['status'] != 'ready-for-review':
            return 3
        return 1 if report.get('cleanup_preflight') == 'blocked' else 0
    except (ValueError, OSError, subprocess.TimeoutExpired, TypeError, KeyError) as e:
        print(json.dumps({'cleanup_preflight': 'blocked', 'error': str(e)}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    sys.exit(main())
