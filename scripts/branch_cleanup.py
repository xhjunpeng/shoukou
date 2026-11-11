#!/usr/bin/env python3
"""Read-only pre/post checks for removing a merged branch while keeping its files.

Task ownership, acceptance and dependency observations are caller evidence.
Passing this check permits neither directory deletion nor source modification.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.dont_write_bytecode = True
import inspect_repo as git_state


def common_dir(repo):
    path = Path(git_state.value(repo, 'rev-parse', '--git-common-dir'))
    return str((Path(repo) / path).resolve() if not path.is_absolute() else path.resolve())


def observe(repo, worktree, base):
    repo = Path(git_state.value(repo, 'rev-parse', '--show-toplevel')).resolve()
    worktree = Path(worktree)
    if not worktree.is_absolute() or worktree.is_symlink():
        raise ValueError('Worktree must be an absolute ordinary directory')
    root = Path(git_state.value(worktree, 'rev-parse', '--show-toplevel')).resolve()
    if root != worktree.resolve() or common_dir(root) != common_dir(repo):
        raise ValueError('Worktree must be the exact root in the selected repository')
    blockers = []
    trees = git_state.worktrees(repo)
    if root == Path(trees[0]['worktree']).resolve():
        blockers.append('primary-checkout-must-use-normal-closeout')
    if not any(Path(t['worktree']).resolve() == root for t in trees):
        blockers.append('worktree-not-registered')
    for marker in ['MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge', 'sequencer']:
        path = Path(git_state.value(root, 'rev-parse', '--git-path', marker))
        if (path if path.is_absolute() else root / path).exists():
            blockers.append('git-operation-in-progress')
    flags = git_state.git(root, 'ls-files', '-v', '-z').split(b'\0')
    if any(f and (f[:1].islower() or f[:1] == b'S') for f in flags):
        blockers.append('hidden-index-state')
    sparse = git_state.run(['git', '-C', str(root), 'config', '--bool', '--get', 'core.sparseCheckout'])
    if sparse.returncode not in (0, 1) or sparse.stdout.strip() == b'true':
        blockers.append('sparse-checkout-not-supported')
    index = git_state.git(root, 'ls-files', '--stage', '-z')
    for entry in index.split(b'\0'):
        if not entry:
            continue
        mode, _, stage = entry.split(b'\t', 1)[0].split()
        if mode == b'160000' or stage != b'0':
            blockers.append('gitlink-or-unmerged-index')
    staged = git_state.paths(root, 'diff', '--cached', '--name-only', '-z')
    dirty = sorted(set(git_state.paths(root, 'diff', '--name-only', '-z')) |
                   set(git_state.paths(root, 'ls-files', '--others', '--exclude-standard', '-z')))
    names = sorted(set(git_state.paths(root, 'ls-files', '--cached', '-z')) |
                   set(git_state.paths(root, 'ls-files', '--others', '--exclude-standard', '-z')))
    files = {}
    digest = hashlib.sha256(index)
    digest.update(git_state.git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all'))
    for name in names:
        path = root / name
        try:
            info = path.lstat()
            mode = '100755' if info.st_mode & 0o111 else '100644'
            if stat.S_ISREG(info.st_mode):
                raw = path.read_bytes()
                content = hashlib.sha256(raw).hexdigest()
                blob = git_state.value(root, 'hash-object', '--no-filters', '--', name)
            else:
                mode, blob = 'unsupported', ''
                content = os.readlink(path) if stat.S_ISLNK(info.st_mode) else str(info.st_mode)
                if name in dirty:
                    blockers.append('unsupported-source-type:' + name)
        except FileNotFoundError:
            mode, blob, content = 'missing', '', ''
            blockers.append('unsupported-source-type:' + name)
        files[name] = {'mode': mode, 'blob': blob}
        digest.update(json.dumps([name, mode, content], ensure_ascii=True).encode())
    target = {'path': str(root), 'git_common_dir': common_dir(root),
              'head': git_state.value(root, 'rev-parse', 'HEAD'),
              'branch': git_state.value(root, 'branch', '--show-current'),
              'base_ref': base, 'base_head': git_state.value(repo, 'rev-parse', base + '^{commit}'),
              'content_fingerprint': digest.hexdigest()}
    return {'schema_version': 1, 'observed_at_unix': time.time(), 'target': target,
            'dirty_paths': dirty, 'staged_paths': staged, 'files': files,
            'blockers': sorted(set(blockers))}


def check(repo, worktree, branch, base, evidence, preserved=False):
    if not isinstance(evidence, dict) or evidence.get('schema_version') != 1:
        raise ValueError('Unsupported branch cleanup evidence')
    if git_state.run(['git', 'check-ref-format', 'refs/heads/' + branch]).returncode:
        raise ValueError('Invalid branch name')
    state = observe(repo, worktree, base)
    current = state['target']
    before = evidence.get('target', {})
    if not isinstance(before, dict):
        raise ValueError('Evidence target must be an object')
    blockers = list(state['blockers'])
    if evidence.get('mode') != 'full' or evidence.get('cleanup_authorized') is not True:
        blockers.append('full-closeout-not-authorized')
    task = evidence.get('task', {})
    if (not isinstance(task, dict) or not all(task.get(k) is True for k in ['ownership_confirmed', 'completed', 'accepted'])
            or not isinstance(task.get('source'), str) or not task['source'].strip()):
        blockers.append('task-ownership-or-completion-unconfirmed')
    dependencies = evidence.get('dependencies', {})
    if (not isinstance(dependencies, dict) or dependencies.get('checked') is not True
            or dependencies.get('active_writers') != [] or dependencies.get('requires_branch') is not False
            or not isinstance(dependencies.get('source'), str) or not dependencies['source'].strip()):
        blockers.append('branch-dependencies-or-writers-unresolved')
    head = before.get('head')
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise ValueError('Evidence must identify an immutable original head')
    if not git_state.commit_in_base(repo, head, current['base_head']):
        blockers.append('original-branch-head-not-in-base')
    if preserved:
        expected = dict(before, branch='')
        if current.get('content_fingerprint') != before.get('content_fingerprint'):
            blockers.append('source-not-preserved')
        if current != expected:
            blockers.append('preserved-directory-or-head-changed')
        exists = git_state.run(['git', '-C', str(repo), 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch])
        if exists.returncode not in (0, 1):
            raise ValueError('Cannot query branch deletion result')
        if exists.returncode == 0 or current['branch']:
            blockers.append('branch-or-checkout-still-present')
    else:
        observed = evidence.get('observed_at_unix')
        if type(observed) not in (int, float) or not -5 <= time.time() - observed <= 120:
            blockers.append('observation-not-fresh')
        if current != before:
            blockers.append('target-changed-since-evidence')
        if current['branch'] != branch:
            blockers.append('target-not-on-requested-branch')
    if before.get('branch') != branch:
        blockers.append('evidence-not-bound-to-requested-branch')
    if state['staged_paths']:
        blockers.append('staged-work-requires-normal-delivery')
    commits = evidence.get('delivered_file_commits')
    if not isinstance(commits, dict) or set(commits) != set(state['dirty_paths']):
        blockers.append('delivered-file-list-not-exact')
        commits = {}
    for name, commit in commits.items():
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit) or not git_state.commit_in_base(repo, commit, current['base_head']):
            blockers.append('file-version-not-in-base:' + name)
            continue
        tree = git_state.git(repo, '--literal-pathspecs', 'ls-tree', '-z', commit, '--', name).split(b'\0')
        expected = None
        if len(tree) == 2 and tree[-1] == b'':
            metadata, entry_name = tree[0].split(b'\t', 1)
            mode, kind, blob = metadata.decode().split()
            if kind == 'blob' and os.fsdecode(entry_name) == name:
                expected = {'mode': mode, 'blob': blob}
        if state['files'].get(name) != expected:
            blockers.append('file-not-in-delivered-version:' + name)
    # A long content check must not certify a changed tree or baseline.
    if observe(repo, worktree, base)['target'] != current:
        blockers.append('target-changed-during-check')
    return {'schema_version': 1, 'branch_cleanup': 'blocked' if blockers else ('preserved' if preserved else 'passed'),
            'target': current, 'blockers': sorted(set(blockers)),
            'boundary': 'Branch reference only; no mutation. Does not authenticate caller task evidence, authorize directory cleanup, or prove functional delivery from matching blobs alone.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--worktree', required=True)
    parser.add_argument('--base', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--observe', action='store_true')
    mode.add_argument('--evidence')
    parser.add_argument('--branch')
    parser.add_argument('--verify-preserved', action='store_true')
    args = parser.parse_args()
    try:
        if args.observe:
            if args.verify_preserved:
                raise ValueError('--verify-preserved requires evidence')
            result = observe(args.repo, args.worktree, args.base)
        else:
            path = Path(args.evidence)
            if (not args.branch or not path.is_absolute() or path.is_symlink()
                    or git_state.inside(path.resolve(), Path(args.worktree).resolve())):
                raise ValueError('Evidence requires branch and an ordinary absolute file outside the worktree')
            result = check(args.repo, args.worktree, args.branch, args.base,
                           json.loads(path.read_bytes()), args.verify_preserved)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 1 if result.get('blockers') else 0
    except (ValueError, OSError, KeyError, TypeError, git_state.subprocess.TimeoutExpired) as error:
        print(json.dumps({'branch_cleanup': 'blocked', 'error': str(error)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
