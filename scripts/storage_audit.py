#!/usr/bin/env python3
"""Read-only full-directory accounting, separate from Git-ignored data checks.

Records all entries without following symlinks. Never deletes, archives or stops
anything. Ownership, dependency queries and retention reasons remain evidence
the caller must establish; filesystem measurements do not authenticate them.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

sys.dont_write_bytecode = True
import inspect_repo as git_state
import closeout_progress as progress
import retained_live


TEMPORARY = {'cache', 'test-data', 'build', 'copy', 'backup', 'audit'}
KINDS = TEMPORARY | {'source', 'git-metadata', 'runtime', 'business-data', 'evidence', 'unknown'}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def digest(path):
    return git_state.file_digest(path)


def normalize_roots(paths):
    roots = []
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('Storage roots must be absolute ordinary directories, not symlinks')
        path = path.resolve()
        if path not in roots:
            roots.append(path)
    # Count overlapping paths once; the containing root already includes them.
    return [p for p in roots if not any(p != q and p.is_relative_to(q) for q in roots)]


def project_roots(repo, extras=(), worktree=None, allow_removed=False):
    repo = Path(git_state.value(repo, 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git_state.value(repo, 'rev-parse', '--git-common-dir'))
    common = (repo / common).resolve() if not common.is_absolute() else common.resolve()
    trees = git_state.worktrees(repo)
    if worktree is None:
        paths = [Path(tree['worktree']) for tree in trees if not tree.get('bare')]
        paths.extend([repo, common, *map(Path, extras)])
    else:
        chosen = Path(worktree)
        if (not chosen.is_absolute() or '..' in chosen.parts
                or any(p.is_symlink() for p in (chosen, *chosen.parents))):
            raise ValueError('Selected storage worktree must be an ordinary absolute path')
        chosen = chosen.resolve()
        registered = any(Path(t['worktree']).resolve() == chosen for t in trees if not t.get('bare'))
        if not registered and not allow_removed:
            raise ValueError('Selected storage worktree is not registered in this repository')
        if chosen.exists():
            top = Path(git_state.value(chosen, 'rev-parse', '--show-toplevel')).resolve()
            owner = Path(git_state.value(chosen, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            if top != chosen or owner != common:
                raise ValueError('Selected storage path changed repository identity')
        elif not allow_removed:
            raise ValueError('Selected storage worktree is missing before capture')
        paths = [chosen, common, *map(Path, extras)]
    return str(common), normalize_roots(paths)


def measure(root):
    """lstat walk; inaccessible/changing/mounted subtrees are explicit gaps."""
    entries, errors, volumes = {}, [], {}
    if not root.exists() and not root.is_symlink():
        return {'path': str(root), 'exists': False, 'entries': {}, 'errors': [],
                'usage': {'logical_bytes': 0, 'allocated_bytes': 0, 'entry_count': 0}, 'volumes': []}
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Storage root is no longer an ordinary directory: ' + str(root))
    initial = root.lstat()
    root_allocated = initial.st_blocks * 512
    seen = {(initial.st_dev, initial.st_ino)}
    logical, allocated = 0, root_allocated
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            if directory.is_symlink() or not directory.is_dir():
                errors.append({'path': str(directory), 'error': 'Directory changed during traversal'})
                continue
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda item: item.name)
        except OSError as error:
            errors.append({'path': str(directory), 'error': str(error)})
            continue
        for child in children:
            path = Path(child.path)
            name = path.relative_to(root).as_posix()
            try:
                info = child.stat(follow_symlinks=False)
                # mode, logical size, mtime, device, inode, allocated bytes.
                row = [info.st_mode, info.st_size, info.st_mtime_ns,
                       info.st_dev, info.st_ino, info.st_blocks * 512]
                entries[name] = row
                if not stat.S_ISDIR(info.st_mode):
                    logical += info.st_size
                identity = (info.st_dev, info.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    allocated += row[5]
                if stat.S_ISDIR(info.st_mode):
                    if info.st_dev != initial.st_dev:
                        errors.append({'path': str(path), 'error': 'Mounted subtree not traversed; review separately'})
                    else:
                        stack.append(path)
            except OSError as error:
                errors.append({'path': str(path), 'error': str(error)})
    fs = os.statvfs(root)
    volumes[initial.st_dev] = {'device': initial.st_dev, 'available_bytes': fs.f_bavail * fs.f_frsize}
    return {'path': str(root), 'exists': True, 'entries': entries, 'errors': errors,
            'root_identity': [initial.st_dev, initial.st_ino, root_allocated],
            'usage': {'logical_bytes': logical, 'allocated_bytes': allocated, 'entry_count': len(entries)},
            'volumes': list(volumes.values())}


def totals(roots):
    seen, logical, allocated = set(), 0, 0
    for root in roots:
        logical += root['usage']['logical_bytes']
        rows = [root['root_identity']] if root['exists'] else []
        rows += [[r[3], r[4], r[5]] for r in root['entries'].values()]
        for device, inode, blocks in rows:
            if (device, inode) not in seen:
                seen.add((device, inode))
                allocated += blocks
    return {'logical_bytes': logical, 'allocated_bytes': allocated,
            'entry_count': sum(r['usage']['entry_count'] for r in roots)}


def capture(repo, extras=(), worktree=None):
    common, paths = project_roots(repo, extras, worktree=worktree)
    future = {Path(p).resolve() for p in extras}
    if any(not p.is_dir() and p not in future for p in paths):
        raise ValueError('A registered or extra storage root is missing; resolve scope before capture')
    roots = [measure(p) for p in paths]
    return {'schema_version': 1, 'kind': 'selected-worktree-storage' if worktree is not None else 'full-project-storage',
            'selected_worktree': str(Path(worktree).resolve()) if worktree is not None else None,
            'registered_worktrees': [str(Path(t['worktree']).resolve()) for t in git_state.worktrees(repo) if not t.get('bare')],
            'git_common_dir': common, 'observed_at_unix': time.time(),
            'extra_roots': [str(Path(p).resolve()) for p in extras],
            'roots': roots, 'totals': totals(roots),
            'measurement_note': 'All entries including nested repositories, ignored data and Git metadata. No symlink traversal. Allocated blocks deduplicate hard links across roots; APFS clones/compression may share blocks. No physical-release guarantee.'}


def load(path):
    data = Path(path).read_bytes()
    if data.startswith(b'\x1f\x8b'):
        data = gzip.decompress(data)
    return json.loads(data)


def save(path, value):
    data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if str(path).endswith('.gz'):
        data = gzip.compress(data, mtime=0)
    with Path(path).open('xb') as output:
        output.write(data)


def valid_rule(name):
    path = Path(name)
    return (text(name) and not path.is_absolute() and '..' not in path.parts
            and name not in ('.', './', '/') and '\\' not in name
            and path.as_posix() == name.rstrip('/'))


def classify(names, rules):
    if not isinstance(rules, dict) or any(not valid_rule(name) for name in rules):
        raise ValueError('Use precise relative names or directory prefixes; no whole-root catchall')
    matched = {name: [] for name in rules}
    gaps = []
    for name in names:
        path = Path(name)
        keys = [name, name + '/', *(str(parent) + '/' for parent in path.parents if str(parent) != '.')]
        hits = [key for key in keys if key in rules]
        if len(hits) != 1:
            gaps.append(name)
        else:
            matched[hits[0]].append(name)
    if gaps or any(not names for names in matched.values()):
        raise ValueError('Storage rules omit, overlap or match no entries: ' + repr(gaps[:8]))
    return matched


def verify(repo, review):
    started = time.time()
    if not isinstance(review, dict):
        raise ValueError('Full storage review must be an object')
    before_path = Path(review['before_inventory'])
    if not before_path.is_absolute() or before_path.is_symlink() or digest(before_path) != review['before_sha256']:
        raise ValueError('Invalid full storage inventory file or checksum')
    before = load(before_path)
    if before.get('kind') not in ('full-project-storage', 'selected-worktree-storage') or before.get('schema_version') != 1:
        raise ValueError('Use full storage capture for its stated scope, not ignored-file inventory')
    selected = before.get('selected_worktree')
    if before['kind'] == 'selected-worktree-storage':
        if not text(selected) or review.get('scope') != 'selected' or review.get('worktree') != selected:
            raise ValueError('Selected storage capture needs an explicitly matching selected review')
    elif selected is not None:
        raise ValueError('Full-project capture cannot hide a selected scope')
    common, live_paths = project_roots(repo, before['extra_roots'], worktree=selected, allow_removed=True)
    if common != before['git_common_dir']:
        raise ValueError('Full storage inventory belongs to another repository')
    git_admin_paths = [Path(common), *(Path(t['worktree']) / '.git' for t in git_state.worktrees(repo))]
    observed = before.get('observed_at_unix')
    if type(observed) not in (int, float) or not 0 < observed <= time.time() + 5:
        raise ValueError('Invalid storage inventory observation time')
    previous = {r['path']: r for r in before['roots']}
    roots = [Path(p) for p in previous]
    if len(previous) != len(before['roots']) or normalize_roots(roots) != roots:
        raise ValueError('Full storage roots are not unique and disjoint')
    if selected is not None and set(roots) != set(live_paths):
        raise ValueError('Selected storage roots do not exactly match their declared scope')
    if any(before_path.resolve().is_relative_to(p) for p in roots):
        raise ValueError('Full storage inventory must be outside all measured roots')
    rules_by_root = review.get('classifications', {})
    if not isinstance(rules_by_root, dict) or set(rules_by_root) - set(previous):
        raise ValueError('Full storage classifications must cover every measured root')
    missing = set(previous) - set(rules_by_root)
    unfinished = [{'object': 'storage-root:' + path, 'version': review['before_sha256'],
                   'next_action': 'Classify the measured root using its recorded contents and dependencies'}
                  for path in sorted(missing)]
    failures, pending, results, replacements, root_results = [], [], [], [], []
    dynamic_files, dynamic_observations = {}, []
    current_roots = [measure(root) for root in roots]
    for path in live_paths:
        if not any(path.is_relative_to(p) for p in roots):
            pending.append('new-storage-root-not-reviewed:' + str(path))
    for root, current in zip(roots, current_roots):
        old = previous[str(root)]
        if str(root) not in rules_by_root:
            continue
        rules = rules_by_root[str(root)]
        if not isinstance(rules, dict) or any(not valid_rule(name) for name in rules):
            raise ValueError('Use precise relative names or directory prefixes; no whole-root catchall')
        groups = {}
        for name in set(old['entries']) | set(current['entries']):
            if not any(key in rules for key in
                       [name, name + '/', *(str(p) + '/' for p in Path(name).parents if str(p) != '.')]):
                groups.setdefault(name.split('/')[0], []).append(name)
        for group, names in sorted(groups.items()):
            unfinished.append({'object': 'storage-entry:' + str(root / group),
                               'version': review['before_sha256'], 'unclassified_entries': len(names),
                               'sample': sorted(names)[:5],
                               'next_action': 'Determine the unclassified entries\' purpose, dependencies and disposition'})
    if unfinished:
        unfinished.extend({'object': key, 'version': review['before_sha256'],
                           'next_action': 'Review the newly observed storage root'} for key in pending)
        raise progress.IncompleteReview('Full storage classifications must cover every measured root and entry', unfinished)
    for root, current in zip(roots, current_roots):
        old = previous[str(root)]
        root_results.append({'path': str(root), 'before': old['usage'], 'after': current['usage'],
                             'scan_errors_before': old['errors'], 'scan_errors_after': current['errors']})
        if old['errors'] or current['errors']:
            pending.append('storage-scan-incomplete:' + str(root))
        names = set(old['entries']) | set(current['entries'])
        rules = rules_by_root[str(root)]
        matched = classify(names, rules)
        for name, entries in matched.items():
            decision = rules[name]
            key = str(root / name.rstrip('/'))
            if not isinstance(decision, dict):
                raise ValueError('Invalid storage disposition')
            kind, action = decision.get('kind'), decision.get('action')
            if kind not in KINDS or not text(decision.get('source')) or not text(decision.get('reason')):
                failures.append(key + ':disposition-not-explained')
            old_names = [p for p in entries if p in old['entries']]
            live_names = [p for p in entries if p in current['entries']]
            dynamic_ok = False
            if 'dynamic' in decision:
                dynamic_ok, error = retained_live.check(root / name, name, decision,
                    old['entries'].get(name), current['entries'].get(name), started)
                if not dynamic_ok:
                    failures.append(key + ':dynamic-retention-invalid:' + error)
                else:
                    dynamic_files[str(root / name)] = decision
                    dynamic_observations.append(retained_live.observation(
                        root / name, decision, old['entries'][name], current['entries'][name]))
            if action == 'removed':
                if kind not in TEMPORARY or not text(decision.get('disposal_basis')):
                    failures.append(key + ':removal-basis-missing-or-protected-kind')
                if live_names:
                    failures.append(key + ':declared-removed-entries-remain')
            elif action == 'retained':
                retention = decision.get('retention')
                if kind == 'unknown' or retention == 'blocked':
                    pending.append(key)
                elif retention not in ('agreed', 'required'):
                    failures.append(key + ':retention-not-explained')
                if kind in TEMPORARY | {'evidence', 'unknown'}:
                    if not text(decision.get('need')) or not text(decision.get('review')):
                        failures.append(key + ':current-need-and-review-required')
                if not live_names:
                    failures.append(key + ':retained-group-missing')
                if kind in ('source', 'business-data', 'evidence', 'runtime'):
                    for p in old_names:
                        old_row, new_row = old['entries'][p], current['entries'].get(p)
                        if new_row is None or stat.S_IFMT(old_row[0]) != stat.S_IFMT(new_row[0]):
                            failures.append(str(root / p) + ':protected-entry-missing-or-type-changed')
                        elif not dynamic_ok and not stat.S_ISDIR(old_row[0]) and old_row[:3] != new_row[:3]:
                            pending.append(str(root / p) + ':protected-material-changed')
                if kind == 'git-metadata':
                    git_path = root / name.rstrip('/')
                    # Only actual administrative paths get mutable membership;
                    # naming an arbitrary source copy "git-metadata" cannot exempt it.
                    if not any(git_path == p or git_path.is_relative_to(p) for p in git_admin_paths):
                        failures.append(key + ':not-a-project-git-metadata-path')
            elif action == 'git-admin-retired':
                # Git removes common/worktrees after its last linked worktree is removed.
                # This is an observed administrative retirement, NOT a deletion permission
                # and NOT permission to disguise missing source or other Git data.
                previous_registered = before.get('registered_worktrees')
                live_registered = [str(Path(t['worktree']).resolve()) for t in git_state.worktrees(repo) if not t.get('bare')]
                retired_admin = (kind == 'git-metadata' and selected is not None
                    and Path(key) == Path(common) / 'worktrees'
                    and isinstance(previous_registered, list) and len(previous_registered) == 2
                    and len(live_registered) == 1 and previous_registered[0] == live_registered[0]
                    and set(previous_registered) - set(live_registered) == {selected}
                    and bool(old_names) and not live_names
                    and not Path(selected).exists() and not Path(selected).is_symlink())
                if not retired_admin:
                    failures.append(key + ':unsupported-git-admin-retirement')
            elif action == 'superseded':
                replacement = Path(decision.get('replacement', ''))
                sha = decision.get('sha256')
                if kind not in {'evidence', 'audit', 'backup', 'copy'} or not text(decision.get('coverage')):
                    failures.append(key + ':supersession-not-explained')
                if live_names:
                    failures.append(key + ':superseded-entries-remain')
                if (not replacement.is_absolute() or replacement.is_symlink()
                        or not any(replacement.resolve().is_relative_to(p) for p in roots)
                        or not replacement.is_file() or digest(replacement) != sha):
                    failures.append(key + ':replacement-unusable-or-checksum-mismatch')
                else:
                    replacements.append((replacement, sha))
            else:
                failures.append(key + ':unknown-storage-action')
            if kind == 'unknown' and action != 'retained':
                failures.append(key + ':unknown-storage-must-be-retained')
            results.append({'path': key, 'kind': kind, 'action': action,
                            'logical_bytes_before': sum(old['entries'][p][1] for p in old_names if not stat.S_ISDIR(old['entries'][p][0])),
                            'logical_bytes_after': sum(current['entries'][p][1] for p in live_names if not stat.S_ISDIR(current['entries'][p][0]))})
    # Rewalk: evidence hashing and process queries may take time.
    for current in current_roots:
        again = measure(Path(current['path']))
        changed = {name for name in set(current['entries']) | set(again['entries'])
                   if current['entries'].get(name) != again['entries'].get(name)}
        allowed = set()
        declared_names = {name for name in current['entries']
                          if str(Path(current['path']) / name) in dynamic_files}
        for name in changed | declared_names:
            path = Path(current['path']) / name
            decision = dynamic_files.get(str(path))
            if decision:
                ok, error = retained_live.check(path, name, decision,
                    current['entries'].get(name), again['entries'].get(name), started)
                if ok:
                    if name in changed:
                        allowed.add(name)
                    dynamic_observations.append(retained_live.observation(
                        path, decision, current['entries'][name], again['entries'][name]))
                else:
                    failures.append(str(path) + ':dynamic-retention-invalid:' + error)
        for name in changed - allowed:
            if (any(Path(name) in Path(child).parents for child in allowed)
                    and retained_live.directory_metadata_only(current['entries'].get(name), again['entries'].get(name))):
                allowed.add(name)
        if changed - allowed or again['exists'] != current['exists'] or again['errors']:
            failures.append('storage-changed-during-verification:' + current['path'])
    if digest(before_path) != review['before_sha256']:
        failures.append('storage-before-inventory-changed')
    for path, sha in replacements:
        if path.is_symlink() or not path.is_file() or digest(path) != sha:
            failures.append('replacement-changed-during-verification:' + str(path))
    after_totals = totals(current_roots)
    old_volumes = {v['device']: v['available_bytes'] for r in before['roots'] for v in r['volumes']}
    new_volumes = {v['device']: v['available_bytes'] for r in current_roots for v in r['volumes']}
    volumes = [{'device': device, 'available_bytes_before': old_volumes.get(device),
                'available_bytes_after': new_volumes.get(device),
                'available_bytes_change': new_volumes[device] - old_volumes[device]
                if device in old_volumes and device in new_volumes else None}
               for device in sorted(old_volumes.keys() | new_volumes.keys())]
    return {'status': 'blocked' if failures else ('partial' if pending else 'complete'),
            'measurement_scope': 'selected-worktree' if selected is not None else 'full-project',
            'selected_worktree': selected,
            'before': before['totals'], 'after': after_totals,
            'logical_bytes_decrease': before['totals']['logical_bytes'] - after_totals['logical_bytes'],
            'allocated_bytes_decrease': before['totals']['allocated_bytes'] - after_totals['allocated_bytes'],
            'groups': results, 'roots': root_results, 'filesystems': volumes,
            'failures': failures, 'pending': pending, 'dynamic_observations': dynamic_observations,
            'execution': progress.from_checks(pending, failures, review['before_sha256'], review.get('stops')),
            'measurement_note': before['measurement_note'],
            'boundary': 'Declarations are not authenticated. No deletion or service action executed. Compare filesystem available space separately; copies, clones and other writers affect it.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    capture_args = sub.add_parser('capture')
    capture_args.add_argument('--repo', required=True)
    capture_args.add_argument('--extra-root', action='append', default=[])
    capture_args.add_argument('--worktree', help='Measure only this registered worktree, common Git metadata and explicit extra roots')
    capture_args.add_argument('--output', required=True)
    verify_args = sub.add_parser('verify')
    verify_args.add_argument('--repo', required=True)
    verify_args.add_argument('--review', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            result = capture(args.repo, args.extra_root, worktree=args.worktree)
            output = Path(args.output)
            if not output.is_absolute() or any(output.resolve().is_relative_to(Path(r['path'])) for r in result['roots']):
                raise ValueError('Save full storage capture outside all measured roots')
            save(output, result)
            print(json.dumps({'inventory': str(output), 'sha256': digest(output), 'totals': result['totals'],
                              'kind': result['kind'], 'selected_worktree': result.get('selected_worktree'),
                              'scan_errors': sum(len(r['errors']) for r in result['roots'])}))
            return 1 if any(r['errors'] for r in result['roots']) else 0
        review = load(args.review)
        result = verify(args.repo, review)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result['execution']['state'] == 'continue' else (0 if result['status'] == 'complete' else 1)
    except progress.IncompleteReview as error:
        try:
            execution = progress.assess(error.items, review.get('stops'))
        except (ValueError, TypeError) as invalid:
            print(json.dumps({'status': 'blocked', 'error': str(invalid)}, ensure_ascii=False))
            return 2
        print(json.dumps({'status': 'incomplete', 'error': str(error), 'execution': execution}, ensure_ascii=False))
        return 3 if execution['state'] == 'continue' else 1
    except (ValueError, OSError, KeyError, TypeError, git_state.subprocess.TimeoutExpired) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
