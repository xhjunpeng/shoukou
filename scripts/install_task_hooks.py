#!/usr/bin/env python3
"""Install version-bound native hooks after local-engine acceptance; never fake trust."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import os
import fcntl
import tempfile
import manage_task_hooks as management

sys.dont_write_bytecode = True
import task_guard as guard

HERE = Path(__file__).resolve().parent
RUNTIME_FILES = ('task_hooks.py', 'task_guard.py', 'closeout_progress.py',
                 'readonly_query.py', 'manage_task_hooks.py')
REQUIRED = {
    'test_omitted_checker_is_automatically_called_and_leaks_force_continuation',
    'test_small_edit_cannot_start_heavy_suite',
    'test_unsaved_source_is_blocked_then_focused_test_is_automatically_routed',
    'test_old_branch_is_not_hidden_by_stage_end',
    'test_preexisting_other_work_is_preserved_by_native_edit_denial',
    'test_original_main_sync_is_checked_without_calling_checker',
    'test_failed_command_cannot_be_hidden_by_unrelated_success',
    'test_same_native_hook_reuses_other_project_without_pnpm',
    'test_live_foundation_automatic_light_check',
    'test_sed_write_with_tab_cannot_skip_automatic_baseline',
    'test_resume_keeps_old_cross_worktree_unsaved_work',
    'test_negative_checker_instruction_cannot_disable_old_task_audit',
    'test_pending_native_command_cannot_finish_before_post_event',
    'test_native_patch_is_automatically_checked_and_other_work_is_preserved',
    'test_native_disable_restore_and_uninstall_preserve_other_hook',
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze_runtime(home, report):
    """Copy exactly the accepted bytes; never import or overwrite a dev checkout."""
    payload = {}
    for name in RUNTIME_FILES:
        path = guard.no_links(HERE / name)
        data = path.read_bytes()
        guard.require(hashlib.sha256(data).hexdigest() == report['candidates'].get(str(path)),
                      'Accepted runtime source changed: ' + name)
        payload[name] = data
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    version = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest = {'format': 'codex-task-hook-runtime-v1', 'version': version, 'files': hashes}
    parent = guard.no_links(Path(home) / 'task-hooks/releases')
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = guard.no_links(parent / version)
    if target.exists():
        guard.require(target.is_dir() and {p.name for p in target.iterdir()} == set(RUNTIME_FILES) | {'RUNTIME.json'},
                      'Existing runtime contains unknown files; nothing overwritten')
        guard.require(guard.regular_json(target / 'RUNTIME.json') == manifest
                      and all(digest(guard.no_links(target / n)) == h for n, h in hashes.items()),
                      'Existing frozen runtime changed; nothing overwritten')
        return target
    temporary = Path(tempfile.mkdtemp(prefix='.runtime-', dir=parent))
    try:
        for name, data in payload.items():
            file = temporary / name
            file.write_bytes(data); file.chmod(0o444)
        guard.atomic(temporary / 'RUNTIME.json', manifest)
        (temporary / 'RUNTIME.json').chmod(0o444)
        guard.require(not target.exists() and not target.is_symlink(), 'Runtime appeared concurrently')
        temporary.rename(target)
        target.chmod(0o555)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    guard.require(all(digest(target / n) == h for n, h in hashes.items()), 'Frozen copy does not match acceptance')
    return target


def validation(path, expected_sha, repo):
    report = guard.regular_json(path)
    guard.require(digest(path) == expected_sha, 'Original native acceptance report hash mismatch')
    guard.require(report.get('format') == 'codex-task-hooks-validation-v1'
                  and report.get('repo') == str(repo) and report.get('exit_code') == 0
                  and report.get('candidate_stable') is True, 'Native acceptance did not pass for this exact repository/version')
    for file, value in report.get('candidates', {}).items():
        guard.require(digest(guard.no_links(Path(file))) == value, 'Validated candidate changed: ' + file)
    required_sources = {str(HERE / f) for f in ['task_hooks.py', 'task_guard.py', 'closeout_progress.py']}
    guard.require(required_sources <= set(report.get('candidates', {})), 'Missing validated checker source')
    cases = {item['name'].rsplit('.', 1)[-1]: item for item in report.get('cases', [])}
    guard.require(REQUIRED <= set(cases), 'Missing required native counterexample or live project acceptance')
    for name in REQUIRED:
        case = cases[name]
        guard.require(case.get('status') == 'passed' and case.get('native'), 'Native case did not pass: ' + name)
        for run in case['native']:
            guard.require(run.get('exit_code') == 0 and run.get('request_count', 0) > 0
                          and run.get('control', {}).get('task_started') is True,
                          'Missing real native event record: ' + name)
    query = cases.get('test_native_readonly_queries_allow_dirty_paths_without_baseline', {})
    guard.require(query.get('status') == 'passed' and query.get('native')
                  and all(x.get('exit_code') == 0 and x.get('request_count', 0) > 0
                          and x.get('control', {}).get('baseline_captures') == 0
                          and x.get('control', {}).get('task_started') is False for x in query['native']),
                  'Missing actual native read-only query acceptance')
    for name in ['test_dry_run_disable_restore_merge_and_uninstall_are_scoped',
                 'test_interrupted_transaction_can_recover_without_overwriting_new_config',
                 'test_missing_receipt_changed_entry_and_symlink_are_refused',
                 'test_source_drift_blocks_restore_but_never_blocks_disable', 'test_hidden_writes_are_not_readonly',
                 'test_restore_recovery_rechecks_source_and_can_be_safely_cancelled',
                 'test_duplicate_entries_cannot_be_reported_as_disabled']:
        guard.require(cases.get(name, {}).get('status') == 'passed', 'Missing management/read-only counterexample: ' + name)
    core = cases.get('test_unknown_process_group_never_completes_reclaimed_interruption', {})
    details = core.get('core') or {}
    checks = details.get('unknown_groups', [])
    guard.require(core.get('status') == 'passed' and details.get('normal', {}).get('state') == 'complete'
                  and {x.get('input') for x in checks} == {'missing', 'null', 'zero', 'negative', 'boolean', 'text'}
                  and all(x.get('result', {}).get('state') != 'complete'
                          and x.get('result', {}).get('reconciled_run_resources') == [] for x in checks),
                  'Missing core counterexample: unknown process group must remain unfinished')
    for name in ['test_development_edits_do_not_change_frozen_checks',
                 'test_global_management_alias_is_synchronous_with_frozen_runtime',
                 'test_installed_definitions_load_without_context_warnings']:
        case = cases.get(name, {})
        guard.require(case.get('status') == 'passed' and case.get('native')
                      and all(x.get('exit_code') == 0 and x.get('request_count', 0) > 0 for x in case['native']),
                      'Missing frozen-runtime native counterexample: ' + name)
    tamper = cases.get('test_frozen_core_is_checked_before_import', {})
    guard.require(tamper.get('status') == 'passed' and tamper.get('native')
                  and all(x.get('exit_code') == 0 and x.get('request_count') == 0 for x in tamper['native'])
                  and (tamper.get('core') or {}).get('bootstrap_denial', {}).get('permissionDecision') == 'deny',
                  'Missing pre-import rejection and actual native pre-model rejection')
    for name in ['test_freeze_requires_exact_accepted_bytes_and_never_overwrites_existing_release',
                 'test_restore_rejects_unknown_runtime_member_but_uninstall_remains_safe']:
        guard.require(cases.get(name, {}).get('status') == 'passed', 'Missing immutable runtime safety check: ' + name)
    engine = guard.no_links(Path(report['engine']).resolve())
    version = subprocess.check_output([str(engine), '--version'], text=True, timeout=10).strip()
    guard.require(version == report.get('engine_version'), 'Native engine changed since acceptance')
    return report


def _install(home, repo, report_path, report_sha, promote=False):
    home = guard.no_links(Path(home).absolute())
    repo = guard.root_of(repo)
    report_path = guard.no_links(Path(report_path).absolute())
    report = validation(report_path, report_sha, repo)
    global_file = home / 'hooks.json'
    state_file = home / 'task-hooks/management.json'
    if state_file.exists():
        guard.require(not guard.regular_json(state_file).get('pending'), 'Recover interrupted management before installing')
    previous_hash = digest(global_file) if global_file.exists() else None
    previous = guard.regular_json(global_file) if global_file.exists() else {'hooks': {}}
    old_receipt_path = home / 'task-hooks/installation.json'
    prior_groups = {}
    if old_receipt_path.exists():
        management.manage(home, 'status')  # exact ownership validation, no writes
        old_receipt = guard.regular_json(old_receipt_path)
        prior_groups = (guard.regular_json(state_file)['managed_groups'] if state_file.exists()
                        else management.definitions(old_receipt, previous, global_file.read_bytes() if global_file.exists() else None))
    guard.require(isinstance(previous, dict) and isinstance(previous.get('hooks', {}), dict), 'Existing hook configuration is not an object')
    base = home / 'task-hooks'; guard.no_links(base); base.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = base / 'sessions'
    runtime = freeze_runtime(home, report)
    handler = runtime / 'task_hooks.py'
    pinned = [{'path': str(runtime / name), 'sha256': digest(runtime / name)} for name in RUNTIME_FILES]
    settings = {'schema_version': 1, 'scope': 'configured' if promote else 'pilot',
                'pilot_roots': [str(repo)], 'heavy_tests': ['ai'], 'pinned_files': pinned,
                'protected_files': [str(global_file), str(home / 'config.toml')],
                'validation': {'path': str(report_path), 'sha256': report_sha,
                               'engine_version': report['engine_version']}}
    settings['runtime_release'] = str(runtime)
    settings['runtime_manifest_sha256'] = digest(runtime / 'RUNTIME.json')
    settings['management_entry'] = {'path': str(home / 'skills/shoukou/scripts/manage_task_hooks.py'),
                                    'sha256': digest(runtime / 'manage_task_hooks.py')}
    content = (json.dumps(settings, ensure_ascii=True, indent=2) + '\n').encode()
    policy_hash = hashlib.sha256(content).hexdigest()
    policies = base / 'policies'; guard.no_links(policies); policies.mkdir(mode=0o700, exist_ok=True)
    policy = policies / (policy_hash + '.json')
    python = shutil.which('python3')
    guard.require(python is not None, 'Python interpreter missing')
    cmd = shlex.join([python, '-I', '-B', str(handler), '--policy', str(policy),
                      '--policy-sha256', policy_hash, '--state-root', str(state)])
    owned_groups = {}
    old_events = previous.setdefault('hooks', {})
    for event in ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'Interrupt']:
        groups = old_events.setdefault(event, [])
        guard.require(isinstance(groups, list), 'Existing hook group must be a list')
        preserved = [g for g in groups if g not in prior_groups.get(event, [])]
        timeout = 3 if event == 'Interrupt' else (30 if event in ('PreToolUse', 'Stop') else 15)
        entry = {'type': 'command', 'command': cmd, 'timeout': timeout}
        if event in {'UserPromptSubmit', 'PreToolUse', 'PostToolUse'}:
            entry['additionalContextLimit'] = 800
        managed = {'hooks': [entry]}
        owned_groups[event] = [managed]
        preserved.append(managed)
        old_events[event] = preserved
    guard.require((digest(global_file) if global_file.exists() else None) == previous_hash,
                  'Global hooks changed concurrently; preserve and review the new file')
    if policy.exists():
        guard.require(digest(policy) == policy_hash, 'Immutable policy version was changed')
    else:
        guard.atomic(policy, settings)
    next_hash = hashlib.sha256((json.dumps(previous, ensure_ascii=True, indent=2) + '\n').encode()).hexdigest()
    if global_file.exists() and previous_hash != next_hash:
        backup = base / ('hooks-before-' + str(time.time_ns()) + '.json')
        backup.write_bytes(global_file.read_bytes()); backup.chmod(0o600)
    if previous_hash != next_hash:
        guard.atomic(global_file, previous)
    receipt = {'format': 'codex-task-hooks-install-v1', 'installed_at': time.time(),
               'hooks_file': str(global_file), 'hooks_sha256': digest(global_file),
               'policy': str(policy), 'policy_sha256': digest(policy), 'pinned_files': pinned,
               'validation': settings['validation'], 'scope': settings['scope'],
               'activation': 'awaiting-native-review', 'managed_groups': owned_groups,
               'runtime_release': str(runtime),
               'runtime_manifest': {'path': str(runtime / 'RUNTIME.json'),
                                    'sha256': digest(runtime / 'RUNTIME.json')},
               'boundary': 'No trust store, sandbox permissions, repository protections, production credentials or other task results were modified. Ordinary user hooks require Codex review; this is not an OS enforcement boundary.'}
    guard.atomic(base / 'installation.json', receipt)
    if state_file.exists():
        guard.no_links(state_file)
        state_file.rename(base / ('management-superseded-' + str(time.time_ns()) + '.json'))
    return receipt


def install(home, repo, report_path, report_sha, promote=False):
    # Serialize with disable/restore/uninstall; no trust-store modification.
    validation(guard.no_links(Path(report_path).absolute()), report_sha, guard.root_of(repo))
    base = management.safe(management.safe(home) / 'task-hooks')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(management.safe(base / 'management.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _install(home, repo, report_path, report_sha, promote)
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True); parser.add_argument('--repo', required=True)
    parser.add_argument('--validation', required=True); parser.add_argument('--sha256', required=True)
    parser.add_argument('--promote', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.home, args.repo, args.validation, args.sha256, args.promote), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'state': 'not-installed', 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)
