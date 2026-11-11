"""Read-only reconciliation of task-local ignored data and service observations.

Classification, ownership and query completeness are supplied by the caller.
This module neither authorizes nor executes deletion or service shutdown.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import inspect_repo as git_state
import retained_live


def text(value):
    return isinstance(value, str) and bool(value.strip())


def alive(pid):
    try:
        os.kill(pid, 0)  # Existence probe only; sends no signal.
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def services_check(services):
    failures, pending = [], []
    if not isinstance(services, dict):
        return [], ['runtime-services-not-assessed']
    if not text(services.get('source')) or services.get('complete') is not True:
        pending.append('runtime-service-query-incomplete')
    observed = services.get('observed_at_unix')
    if (type(observed) not in (int, float) or not -5 <= time.time() - observed <= 120):
        pending.append('runtime-service-query-not-fresh')
    items = services.get('items')
    if not isinstance(items, list):
        raise ValueError('Runtime services must include an items list')
    names = set()
    for item in items:
        if not isinstance(item, dict) or not text(item.get('name')) or item['name'] in names:
            raise ValueError('Runtime service names must be nonempty and unique')
        names.add(item['name'])
        key = 'service:' + item['name']
        outcome = item.get('outcome')
        if outcome not in ('stopped', 'retained', 'unknown'):
            raise ValueError('Unknown runtime service outcome')
        if not text(item.get('reason')) or not text(item.get('source')):
            failures.append(key + ':outcome-not-explained')
        if outcome == 'stopped':
            pid = item.get('pid')
            if type(pid) is not int or not 0 < pid <= 2147483647:
                failures.append(key + ':stopped-pid-not-recorded')
            elif alive(pid):
                failures.append(key + ':declared-stopped-process-still-exists')
        elif outcome == 'unknown' or item.get('retention') == 'blocked':
            pending.append(key)
        elif item.get('retention') not in ('agreed', 'required'):
            failures.append(key + ':retention-not-explained')
        if outcome == 'retained' and not text(item.get('review')):
            failures.append(key + ':next-review-not-recorded')
    return failures, pending


def verify(repo, ledger):
    started = time.time()
    failures, pending, results, checkpoints, replacements = [], [], [], [], []
    dynamic_files, dynamic_observations = {}, []
    runtime = ledger.get('runtime')
    if runtime is None:
        return {'status': 'not-assessed', 'roots': [], 'services': None}, [], ['runtime-resources-not-assessed']
    if not isinstance(runtime, dict):
        raise ValueError('Runtime review must be an object')
    roots = [Path(ledger['main_path']).resolve()]
    roots.extend(Path(item['path']).resolve() for item in ledger['directories'])
    if any(git_state.inside(a, b) or git_state.inside(b, a)
           for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ValueError('Runtime roots must be disjoint')
    before_path = Path(runtime['before_inventory'])
    if not before_path.is_absolute() or before_path.is_symlink() or any(git_state.inside(before_path.resolve(), root) for root in roots):
        raise ValueError('Before inventory must be an ordinary file outside all reviewed roots')
    raw = before_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != runtime.get('before_sha256'):
        raise ValueError('Before inventory checksum does not match')
    before = json.loads(raw)
    if not isinstance(before, dict) or before.get('schema_version') != 1:
        raise ValueError('Unsupported before inventory')
    common = Path(git_state.value(repo, 'rev-parse', '--git-common-dir'))
    common = (Path(repo) / common).resolve() if not common.is_absolute() else common.resolve()
    if before.get('git_common_dir') != str(common):
        raise ValueError('Before inventory belongs to a different repository')
    observed = before.get('observed_at_unix')
    if type(observed) not in (int, float) or not 0 < observed <= time.time() + 5:
        raise ValueError('Before inventory timestamp is invalid')
    states = {}
    for state in before['worktrees']:
        path = Path(state['path'])
        canonical = str(path.resolve())
        if not path.is_absolute() or canonical in states:
            raise ValueError('Before inventory roots must be absolute and unique')
        states[canonical] = state
    declared = runtime.get('classifications')
    if not isinstance(declared, dict):
        raise ValueError('Runtime classifications must be an object')
    classifications = {}
    for name, rules in declared.items():
        path = Path(name)
        canonical = str(path.resolve())
        if not path.is_absolute() or canonical in classifications:
            raise ValueError('Runtime classification roots must be absolute and unique after resolving aliases')
        classifications[canonical] = rules
    if set(classifications) != {str(root) for root in roots}:
        raise ValueError('Classifications must cover main and every declared task directory exactly')
    totals = {'logical_bytes_before': 0, 'logical_bytes_after': 0,
              'allocated_bytes_before': 0, 'allocated_bytes_after': 0,
              'archived_logical_bytes': 0}
    volumes = {}
    for root in roots:
        key = 'runtime:' + str(root)
        old = states.get(str(root))
        if (not isinstance(old, dict) or not isinstance(old.get('ignored'), list)
                or 'ignored_usage' not in old or 'ignored_metadata' not in old):
            raise ValueError('Before inventory is incomplete; use full output, not summary')
        names = old['ignored']
        if any(not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts for name in names):
            raise ValueError('Invalid ignored path in before inventory')
        old_metadata = {item[0]: item[1:] for item in old['ignored_metadata']}
        if set(old_metadata) != set(names):
            raise ValueError('Before inventory file metadata is incomplete')
        if root.is_symlink():
            raise ValueError('Runtime root changed into a symlink')
        if root.exists():
            current = git_state.snapshot(root)
            expected_head = (ledger['base']['head'] if root == roots[0] else
                             next(item['head'] for item in ledger['directories'] if Path(item['path']).resolve() == root))
            if current['head'] != expected_head:
                failures.append(key + ':head-changed-during-verification')
        else:
            current = {'ignored': [], 'ignored_usage': {'logical_bytes': 0, 'allocated_bytes': 0, 'file_count': 0}}
        new_metadata = {item[0]: item[1:] for item in current.get('ignored_metadata', [])}
        checkpoints.append((root, current))
        rules = classifications[str(root)]
        expanded = git_state.classify_ignored(sorted(set(names) | set(current['ignored'])), rules,
                                             directory_actions=('removed', 'retained', 'superseded'))
        if expanded is None:
            failures.append(key + ':ignored-data-not-fully-classified')
        else:
            after_names = set(current['ignored'])
            before_names = set(names)
            matched_by_rule = {}
            for name, decision in expanded.items():
                matched_by_rule.setdefault(id(decision), []).append(name)
            for name, decision in rules.items():
                resource_key = key + ':' + name
                if not isinstance(decision, dict):
                    failures.append(resource_key + ':invalid-disposition')
                    continue
                kind, action = decision.get('kind'), decision.get('action')
                if kind not in ('cache', 'test-data', 'evidence', 'business-data', 'unknown', 'runtime'):
                    failures.append(resource_key + ':unknown-resource-kind')
                if not text(decision.get('reason')) or not text(decision.get('source')):
                    failures.append(resource_key + ':disposition-not-explained')
                dynamic_ok = False
                if 'dynamic' in decision:
                    dynamic_ok, error = retained_live.check(root / name, name, decision,
                        old_metadata.get(name), new_metadata.get(name), started)
                    if not dynamic_ok:
                        failures.append(resource_key + ':dynamic-retention-invalid:' + error)
                    else:
                        info = (root / name).lstat()
                        dynamic_files[str(root / name)] = (decision, info.st_dev, info.st_ino)
                        dynamic_observations.append(retained_live.observation(
                            root / name, decision, old_metadata[name], new_metadata[name]))
                if kind == 'runtime' and not dynamic_ok:
                    failures.append(resource_key + ':runtime-requires-exact-live-file-declaration')
                matched = matched_by_rule[id(decision)]
                if action == 'removed':
                    if kind not in ('cache', 'test-data') or not text(decision.get('rebuild')):
                        failures.append(resource_key + ':removal-not-proven-rebuildable')
                    if any(path in after_names for path in matched) or (root / name.rstrip('/')).exists() or (root / name.rstrip('/')).is_symlink():
                        failures.append(resource_key + ':declared-removed-data-still-exists')
                elif action == 'retained':
                    if not (root / name.rstrip('/')).exists() and not (root / name.rstrip('/')).is_symlink():
                        failures.append(resource_key + ':retained-data-missing')
                    retained = [path for path in matched if path in before_names]
                    if kind in ('test-data', 'evidence', 'business-data', 'runtime'):
                        if any(path not in after_names for path in retained):
                            failures.append(resource_key + ':retained-material-missing-files')
                        present = [path for path in retained if path in after_names]
                        if any(not stat.S_ISREG(old_metadata[path][0]) or
                               not stat.S_ISREG(new_metadata[path][0]) for path in present):
                            failures.append(resource_key + ':retained-material-not-ordinary-files')
                        if not dynamic_ok and kind in ('evidence', 'business-data', 'runtime') and any(old_metadata[path] != new_metadata[path] for path in present):
                            pending.append(resource_key + ':retained-material-changed-needs-review')
                    retention = decision.get('retention')
                    if kind == 'unknown' or retention == 'blocked':
                        pending.append(resource_key)
                    elif retention not in ('agreed', 'required') or kind in ('cache', 'test-data') and retention == 'required':
                        failures.append(resource_key + ':retention-not-explained')
                    if kind in ('cache', 'test-data', 'unknown') and not text(decision.get('review')):
                        failures.append(resource_key + ':next-review-not-recorded')
                elif action == 'superseded':
                    replacement = Path(decision.get('replacement', ''))
                    digest = decision.get('sha256')
                    if kind != 'evidence' or not text(decision.get('coverage')):
                        failures.append(resource_key + ':supersession-not-explained')
                    if any(path in after_names for path in matched) or (root / name.rstrip('/')).exists() or (root / name.rstrip('/')).is_symlink():
                        failures.append(resource_key + ':superseded-source-still-exists')
                    if (not replacement.is_absolute() or replacement.is_symlink()
                            or not replacement.is_file() or not isinstance(digest, str)
                            or re.fullmatch('[0-9a-f]{64}', digest) is None
                            or git_state.file_digest(replacement) != digest):
                        failures.append(resource_key + ':replacement-unusable-or-checksum-mismatch')
                    else:
                        replacements.append((replacement, digest))
                elif action == 'archived':
                    backup = Path(decision.get('backup', ''))
                    digest = decision.get('sha256')
                    if name in after_names or (root / name).exists() or (root / name).is_symlink():
                        failures.append(resource_key + ':archived-source-still-exists')
                    if (name.endswith('/') or not backup.is_absolute() or backup.is_symlink()
                            or any(git_state.inside(backup.resolve(), other) for other in roots)
                            or not backup.is_file() or not isinstance(digest, str)
                            or re.fullmatch('[0-9a-f]{64}', digest) is None):
                        failures.append(resource_key + ':archive-unusable-or-inside-reviewed-roots')
                    elif git_state.file_digest(backup) != digest:
                        failures.append(resource_key + ':archive-content-mismatch')
                    else:
                        totals['archived_logical_bytes'] += backup.stat().st_size
                else:
                    failures.append(resource_key + ':unknown-resource-action')
                if kind == 'unknown' and action != 'retained':
                    failures.append(resource_key + ':unknown-data-must-be-retained-for-review')
        for when, state in (('before', old), ('after', current)):
            for size in ('logical_bytes', 'allocated_bytes'):
                amount = state['ignored_usage'][size]
                if type(amount) is not int or amount < 0:
                    raise ValueError('Invalid storage amount')
                totals[size + '_' + when] += amount
        previous_volume = old['filesystem']
        device = previous_volume['device']
        probe = root
        while not probe.exists():
            probe = probe.parent
        if probe.stat().st_dev != device:
            failures.append(key + ':filesystem-changed')
        elif device not in volumes:
            filesystem_stat = os.statvfs(probe)
            available = filesystem_stat.f_bavail * filesystem_stat.f_frsize
            volumes[device] = {'device': device, 'available_bytes_before': previous_volume['available_bytes'],
                               'available_bytes_after': available,
                               'available_bytes_change': available - previous_volume['available_bytes']}
        results.append({'path': str(root), 'before': old['ignored_usage'], 'after': current['ignored_usage'],
                        'dispositions': rules})
    service_failures, service_pending = services_check(runtime.get('services'))
    failures.extend(service_failures)
    pending.extend(service_pending)
    # Backup hashing can be slow. Detect changed input or data before reporting.
    if before_path.read_bytes() != raw:
        failures.append('runtime-before-inventory-changed-during-verification')
    for replacement, digest in replacements:
        if replacement.is_symlink() or not replacement.is_file() or git_state.file_digest(replacement) != digest:
            failures.append('runtime-replacement-changed-during-verification:' + str(replacement))
    for root, checkpoint in checkpoints:
        fingerprint = checkpoint.get('fingerprint')
        if fingerprint is None:
            changed = root.exists() or root.is_symlink()
        else:
            changed = not root.exists() or root.is_symlink()
            if not changed:
                final = git_state.snapshot(root)
                changed = final['fingerprint'] != fingerprint
                if dynamic_files:
                    changed = any(final.get(k) != checkpoint.get(k) for k in
                                  ['head', 'branch', 'status', 'changes', 'nested_repositories', 'submodules'])
                    first = {item[0]: item[1:] for item in checkpoint['ignored_metadata']}
                    last = {item[0]: item[1:] for item in final['ignored_metadata']}
                    for name in set(first) | set(last):
                        path = root / name
                        declared = dynamic_files.get(str(path))
                        if first.get(name) == last.get(name) and not declared:
                            continue
                        if declared and name in first and name in last and path.is_file() and not path.is_symlink():
                            decision, device, inode = declared
                            info = path.lstat()
                            ok, error = retained_live.check(path, name, decision,
                                [*first[name], device, inode], [*last[name], info.st_dev, info.st_ino], started)
                            if ok:
                                dynamic_observations.append(retained_live.observation(path, decision, first[name], last[name]))
                                continue
                        changed = True
        if changed:
            failures.append('runtime:' + str(root) + ':changed-during-verification')
    for size in ('logical_bytes', 'allocated_bytes'):
        totals[size + '_decrease'] = totals[size + '_before'] - totals[size + '_after']
    return {'status': 'blocked' if failures else ('partial' if pending else 'complete'),
            'before_inventory': str(before_path), 'roots': results, 'totals': totals,
            'filesystems': list(volumes.values()), 'services': runtime.get('services'),
            'dynamic_observations': dynamic_observations,
            'measurement_note': 'Ignored-file accounting only; allocated totals may share blocks across roots. Directory decrease, archived data and filesystem free-space change are separate observations, not a guarantee of bytes freed by this task.'}, failures, pending
