#!/usr/bin/env python3
"""Read-only final reconciliation of a closeout ledger with live Git state.

This verifies outcomes, not cleanup authorization or app occupancy.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

sys.dont_write_bytecode = True
import inspect_repo as git_state
import runtime_resources
import branch_cleanup
import closeout_progress as progress


def remote_heads(repo):
    response = git_state.run(['git', '-C', str(repo), 'ls-remote', '--heads', 'origin'])
    if response.returncode:
        raise ValueError('Latest origin branches unavailable; final verification cannot pass')
    return {line.split()[1].removeprefix('refs/heads/'): line.split()[0]
            for line in response.stdout.decode().splitlines()}


def local_heads(repo):
    return dict(line.rsplit('|', 1) for line in git_state.value(
        repo, 'for-each-ref', '--format=%(refname:short)|%(objectname)', 'refs/heads').splitlines())


def capture_branches(repo):
    local = local_heads(repo)
    remote = remote_heads(repo) if git_state.value(repo, 'remote') else {}
    if local_heads(repo) != local:
        raise ValueError('Branches changed during initial capture; retry before cleanup')
    return {'schema_version': 1, 'git_common_dir': branch_cleanup.common_dir(repo),
            'observed_at_unix': time.time(), 'local': local, 'remote': remote}


def branch_changes(repo, ledger, local, remote):
    if ledger['mode'] != 'full' or ledger['scope'] != 'all-task-branches':
        return {'status': 'not-required'}, [], []
    review = ledger.get('branch_inventory')
    if review is None:
        return {'status': 'not-assessed'}, [], ['initial-branch-inventory-not-assessed']
    if not isinstance(review, dict):
        raise ValueError('Branch inventory review must be an object')
    path = Path(review['before_inventory'])
    roots = [Path(ledger['main_path']), *(Path(d['path']) for d in ledger['directories'])]
    if not path.is_absolute() or path.is_symlink() or any(git_state.inside(path.resolve(), r.resolve()) for r in roots):
        raise ValueError('Initial branch inventory must be an ordinary file outside reviewed roots')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != review.get('before_sha256'):
        raise ValueError('Initial branch inventory checksum does not match')
    before = json.loads(raw)
    if (not isinstance(before, dict) or before.get('schema_version') != 1
            or before.get('git_common_dir') != branch_cleanup.common_dir(repo)):
        raise ValueError('Initial branch inventory belongs to a different repository or schema')
    observed = before.get('observed_at_unix')
    if type(observed) not in (int, float) or not 0 < observed <= time.time() + 5:
        raise ValueError('Initial branch inventory timestamp is invalid')
    introduced = review.get('introduced', {})
    if not isinstance(introduced, dict) or set(introduced) - {'local', 'remote'}:
        raise ValueError('Introduced branch reviews must be local/remote maps')
    failures, additions = [], {}
    candidates = {c['branch']: c for c in ledger['candidates']}
    target = ledger['base']['branch']
    for location, live in [('local', local), ('remote', remote)]:
        initial = before.get(location)
        if not isinstance(initial, dict):
            raise ValueError('Initial branch inventory must contain local and remote maps')
        for name, head in initial.items():
            if (not isinstance(name, str) or not isinstance(head, str)
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head)
                    or git_state.run(['git', 'check-ref-format', 'refs/heads/' + name]).returncode):
                raise ValueError('Initial branch inventory contains an invalid reference')
        for name in set(initial) - {target} - set(candidates):
            failures.append(location + ':' + name + ':initial-branch-not-accounted-for')
        for name in set(initial) & set(candidates):
            if candidates[name][location]['outcome'] == 'absent':
                failures.append(location + ':' + name + ':initial-branch-cannot-be-declared-absent')
        observed_names = set(live) | {name for name, c in candidates.items()
                                    if c[location]['outcome'] in ('removed', 'retained')}
        added = observed_names - set(initial) - {target}
        additions[location] = sorted(added)
        decisions = introduced.get(location, {})
        if not isinstance(decisions, dict) or set(decisions) - added:
            raise ValueError('Introduced branch review names do not match observed additions')
        for name in sorted(added):
            key = location + ':' + name
            decision = decisions.get(name)
            if not isinstance(decision, dict) or not isinstance(decision.get('source'), str) or not decision['source'].strip():
                failures.append(key + ':new-branch-not-reviewed')
                continue
            declaration = candidates.get(name, {}).get(location, {})
            head = decision.get('head')
            if (not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head)
                    or head != (live.get(name) or declaration.get('head'))):
                failures.append(key + ':new-branch-review-head-mismatch')
            state = decision.get('state')
            outcome, retention = declaration.get('outcome'), declaration.get('retention')
            valid = ((state in ('continuing', 'long-lived') and outcome == 'retained' and retention == 'agreed')
                     or (state == 'paused' and outcome == 'retained' and retention in ('agreed', 'blocked'))
                     or (state == 'unknown' and outcome == 'retained' and retention == 'blocked')
                     or (state == 'completed' and (outcome == 'removed' or outcome == 'retained' and retention == 'blocked')))
            if not valid:
                failures.append(key + ':new-branch-disposition-inconsistent')
    return {'status': 'blocked' if failures else 'assessed', 'introduced': additions,
            'before_inventory': str(path), 'before_sha256': digest}, failures, []


def retained_branch_review(repo, ledger, declaration, name, location, base_head, trees):
    """Reconcile retained-ref evidence, without authenticating caller sources."""
    if ledger['mode'] != 'full':
        return {'status': 'not-required'}, []
    key = location + ':' + name + ':'
    review = declaration.get('review')
    if not isinstance(review, dict):
        return {'status': 'not-assessed'}, [key + 'retained-branch-review-missing']
    failures = []
    head = declaration.get('head')
    if (not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head)
            or review.get('head') != head):
        return {'status': 'blocked'}, [key + 'retained-review-head-mismatch']
    available = git_state.run(['git', '-C', str(repo), 'cat-file', '-e', head + '^{commit}']).returncode == 0
    in_base = git_state.ancestor(repo, head, base_head) if available else None

    def sourced(part):
        return (isinstance(part, dict) and isinstance(part.get('source'), str)
                and bool(part['source'].strip()))

    task = review.get('task')
    delivery = review.get('delivery')
    dependency = review.get('dependency')
    if not sourced(task) or task.get('state') not in ('completed', 'continuing', 'long-lived', 'paused', 'unknown'):
        failures.append(key + 'retained-task-review-invalid')
        task = {}
    if (not sourced(delivery) or type(delivery.get('accepted')) not in (bool, type(None))
            or 'accepted' not in delivery
            or delivery.get('uncommitted') not in ('none', 'pending', 'delivered-copy', 'unknown')):
        failures.append(key + 'retained-delivery-review-invalid')
        delivery = {}
    if (not sourced(dependency) or type(dependency.get('required')) not in (bool, type(None))
            or 'required' not in dependency):
        failures.append(key + 'retained-dependency-review-invalid')
        dependency = {}
    state = task.get('state')
    required = dependency.get('required')
    retention = declaration.get('retention')
    if retention == 'agreed' and (state not in ('continuing', 'long-lived', 'paused') or required is not True):
        failures.append(key + 'retained-task-disposition-inconsistent')
    for tree in trees if location == 'local' and delivery.get('uncommitted') == 'none' else []:
        if (tree.get('branch') == 'refs/heads/' + name
                and git_state.git(tree['worktree'], 'status', '--porcelain=v1', '-z', '--untracked-files=all')):
            failures.append(key + 'retained-uncommitted-state-contradicts-worktree')

    blocker = review.get('blocker')
    if retention == 'blocked':
        fields = ('source', 'finding', 'next_action', 'execution_location', 'release_condition')
        if (not isinstance(blocker, dict)
                or any(not isinstance(blocker.get(f), str) or not blocker[f].strip() for f in fields)):
            failures.append(key + 'branch-blocker-details-missing')
            blocker = {}
        kind = blocker.get('kind')
        if kind == 'directory':
            failures.append(key + 'directory-only-branch-blocker')
        elif kind not in ('acceptance', 'delivery', 'branch-dependency', 'ownership', 'git-state', 'tool'):
            failures.append(key + 'branch-blocker-kind-invalid')
        if kind == 'branch-dependency' and required is not True:
            failures.append(key + 'branch-dependency-blocker-not-supported')
        if kind == 'acceptance' and delivery.get('accepted') is True:
            failures.append(key + 'acceptance-blocker-already-resolved')
        if (kind in ('acceptance', 'delivery', 'branch-dependency') and state == 'completed'
                and in_base is True and delivery.get('accepted') is True
                and delivery.get('uncommitted') in ('none', 'delivered-copy') and required is False):
            failures.append(key + 'branch-retention-blocker-already-resolved')
    return {'status': 'blocked' if failures else 'assessed', 'head': head,
            'head_in_base': in_base, 'task': task, 'delivery': delivery,
            'dependency': dependency, 'blocker': blocker if retention == 'blocked' else None}, failures


def verify(repo, ledger):
    repo = Path(git_state.value(repo, 'rev-parse', '--show-toplevel')).resolve()
    if not isinstance(ledger, dict) or ledger.get('schema_version') not in (1, 2):
        raise ValueError('Unsupported ledger schema')
    if ledger.get('mode') not in ('full', 'stage'):
        raise ValueError('Ledger must declare full or stage closeout')
    if ledger.get('scope') not in ('selected', 'all-task-branches'):
        raise ValueError('Ledger must declare selected or all-task-branches scope')
    base = ledger['base']
    branch = base['branch']
    location = base.get('location', 'remote')
    if location not in ('local', 'remote'):
        raise ValueError('Unknown base location')
    if location == 'local':
        if git_state.value(repo, 'remote'):
            raise ValueError('Local mode requires a repository without remotes')
        if base['ref'] != branch:
            raise ValueError('Local base must identify the declared local branch')
        live_remote = {}
    else:
        if base['ref'] != 'origin/' + branch:
            raise ValueError('Base ref must identify the declared origin branch')
        live_remote = remote_heads(repo)
    base_head = git_state.value(repo, 'rev-parse', '--verify', base['ref'] + '^{commit}')
    failures, pending, results = [], [], []
    if base_head != base['head'] or location == 'remote' and live_remote.get(branch) != base['head']:
        failures.append('base-does-not-match-ledger-and-live-origin')
    local = local_heads(repo)
    trees = git_state.worktrees(repo)
    main = Path(ledger['main_path'])
    if not main.is_absolute():
        raise ValueError('main_path must be absolute')
    main_entry = next((t for t in trees if Path(t['worktree']).resolve() == main.resolve()), None)
    if not main_entry or main_entry.get('branch') != 'refs/heads/' + branch:
        failures.append('main-directory-not-registered-on-target-branch')
    else:
        if main_entry['HEAD'] != base_head:
            failures.append('local-main-not-synchronized')
        if git_state.git(main, 'status', '--porcelain=v1', '-z', '--untracked-files=all'):
            failures.append('main-has-unaccounted-changes')
    candidates = ledger['candidates']
    names = [c['branch'] for c in candidates]
    if len(set(names)) != len(names) or branch in names:
        raise ValueError('Candidate names must be unique and exclude the target branch')
    for candidate in candidates:
        name = candidate['branch']
        if git_state.run(['git', 'check-ref-format', 'refs/heads/' + name]).returncode:
            raise ValueError('Invalid candidate branch')
        for location, refs in [('local', local), ('remote', live_remote)]:
            declaration = candidate[location]
            outcome = declaration['outcome']
            if base.get('location') == 'local' and location == 'remote' and (outcome != 'absent' or declaration.get('head') is not None):
                raise ValueError('Local mode must declare remote refs absent')
            actual = refs.get(name)
            key = location + ':' + name
            review = None
            if outcome not in ('removed', 'absent', 'retained'):
                raise ValueError('Unknown ref outcome: ' + outcome)
            if outcome == 'absent' and declaration.get('head') is not None:
                raise ValueError('Originally absent ref must have null head')
            if outcome in ('removed', 'retained') and not declaration.get('head'):
                raise ValueError('Removed or retained ref must record its original head')
            if outcome in ('removed', 'absent'):
                if actual is not None:
                    failures.append(key + ':ref-still-exists')
                if location == 'local' and any(t.get('branch') == 'refs/heads/' + name for t in trees):
                    failures.append(key + ':still-checked-out')
            else:
                if actual != declaration['head']:
                    failures.append(key + ':retained-head-changed-or-missing')
                if not declaration.get('reason') or declaration.get('retention') not in ('agreed', 'blocked'):
                    failures.append(key + ':retention-not-explained')
                if declaration.get('retention') == 'blocked':
                    pending.append(key)
                review, review_failures = retained_branch_review(
                    repo, ledger, declaration, name, location, base_head, trees)
                failures.extend(review_failures)
            result = {'branch': name, 'location': location, 'declared': outcome, 'actual_head': actual}
            if review is not None:
                result['review'] = review
            results.append(result)
    if ledger.get('scope') == 'all-task-branches':
        for location, refs in [('local', local), ('remote', live_remote)]:
            unexpected = set(refs) - {branch} - set(names)
            failures.extend(location + ':' + name + ':candidate-not-accounted-for' for name in sorted(unexpected))
    directory_paths = set()
    for directory in ledger['directories']:
        path = Path(directory['path'])
        if not path.is_absolute() or path.resolve() == main.resolve() or str(path.resolve()) in directory_paths:
            raise ValueError('Cleanup directory must be absolute, unique and exclude main')
        directory_paths.add(str(path.resolve()))
        registered = next((t for t in trees if Path(t['worktree']).resolve() == path.resolve()), None)
        key = 'directory:' + str(path)
        if directory['outcome'] == 'removed':
            if path.exists() or path.is_symlink() or registered:
                failures.append(key + ':directory-or-registration-remains')
        elif directory['outcome'] == 'retained':
            if not path.is_dir() or not registered or registered['HEAD'] != directory['head']:
                failures.append(key + ':retained-directory-missing-or-head-changed')
            if not directory.get('reason') or directory.get('retention') not in ('agreed', 'blocked'):
                failures.append(key + ':retention-not-explained')
            if directory.get('retention') == 'blocked':
                pending.append(key)
        else:
            raise ValueError('Unknown directory outcome')
    changes, branch_failures, branch_pending = branch_changes(repo, ledger, local, live_remote)
    failures.extend(branch_failures)
    pending.extend(branch_pending)
    runtime, runtime_failures, runtime_pending = runtime_resources.verify(repo, ledger)
    failures.extend(runtime_failures)
    pending.extend(runtime_pending)
    # Runtime and content checks can be slow: don't certify an earlier ref list.
    final_remote = {} if base.get('location', 'remote') == 'local' else remote_heads(repo)
    if local_heads(repo) != local or final_remote != live_remote:
        failures.append('branch-refs-changed-during-final-verification')
    if 'before_inventory' in changes:
        path = Path(changes['before_inventory'])
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != changes['before_sha256']:
            failures.append('initial-branch-inventory-changed-during-verification')
    state = 'blocked' if failures else ('partial' if pending else 'complete')
    versions = {location + ':' + c['branch']: c[location].get('head')
                for c in candidates for location in ('local', 'remote')}
    versions.update({'directory:' + d['path']: d['head'] for d in ledger['directories']})
    execution = progress.from_checks(pending, failures, base_head, ledger.get('stops'), versions)
    return {'schema_version': 2, 'mode': ledger['mode'], 'closeout': state, 'base_head': base_head,
            'live_remote_main': live_remote.get(branch), 'refs': results,
            'runtime': runtime, 'branch_changes': changes,
            'failures': failures, 'pending': pending, 'execution': execution,
            'boundary': 'Live outcome verification only. Ledger reasons are caller declarations; no authorization, acceptance or app occupancy authenticated. No mutation executed.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--ledger')
    mode.add_argument('--capture-branches', action='store_true')
    args = parser.parse_args()
    try:
        if args.capture_branches:
            print(json.dumps(capture_branches(args.repo), ensure_ascii=True, indent=2))
            return 0
        result = verify(args.repo, json.loads(Path(args.ledger).read_text()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result['execution']['state'] == 'continue' else (0 if result['closeout'] == 'complete' else 1)
    except (ValueError, OSError, KeyError, TypeError, git_state.subprocess.TimeoutExpired) as error:
        print(json.dumps({'closeout': 'blocked', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
