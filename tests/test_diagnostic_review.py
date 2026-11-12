"""Exact failed read-only diagnostic migration, correction coverage and resource guards."""
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import os
import re
import tempfile
import shutil
import unittest
from unittest.mock import patch

from test_task_hooks import HookFixture, SCRIPTS
from hook_native_probe import ENGINE, native_run, command, message
from task_hooks import Hooks
import task_guard as guard
import manage_task_hooks as manager
import install_task_hooks as installer
from readonly_query import diagnostic_correction, query_python


class DiagnosticReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture(); self.addCleanup(self.f.close)
        self.inputs = self.f.repo / 'records'; self.inputs.mkdir()
        (self.inputs / 'task.json').write_text('{"value": 7}')
        (self.inputs / 'decisions.json').write_text('[]')
        self.f.git('add', '.'); self.f.git('commit', '-m', 'diagnostic fixture inputs')
        source = ('from pathlib import Path\nimport json\n'
                  f'for p in sorted(Path({str(self.inputs)!r}).glob("*.json")):\n'
                  '    d=json.loads(p.read_text())\n'
                  '    print(p.name,d.get("value"))\n')
        self.original = "python3 -I -B - <<'PY'\n" + source + 'PY'
        self.corrected = self.original.replace('    print', '    if not isinstance(d,dict):continue\n    print')
        self.key = guard.digest(self.original)
        self.hooks = Hooks(self.f.policy, self.f.state)
        self.hooks.handle(self.f.event('PreToolUse', 'python3 -c "import sys; print(1)"'))
        self.hooks.handle(self.f.event('PostToolUse'))
        self.state = self.f.control()
        self.state['command_failures'] = {self.key: {'exit_code': 1, 'tool_use_id': 'legacy-failed-query'}}
        self.original_facts = copy.deepcopy(self.state['command_failures'])
        self.files = {}
        for name, text in [('original', self.original), ('corrected', self.corrected)]:
            p = self.f.root / (name + '.txt'); p.write_text(text); self.files[name] = p
        self.options = manager.parser().parse_args(['--home', str(self.f.home), 'review-diagnostic', '--apply',
            '--repo', str(self.f.repo), '--session', self.state['session_id'], '--task', self.state['task'],
            '--failed', self.key, '--original', str(self.files['original']), '--corrected', str(self.files['corrected']),
            '--reason', 'Skip explicitly reported non-object metadata while retaining every original query operation'])
        self.checkpoint()

    def checkpoint(self):
        key = guard.digest([str(self.f.repo), self.state['session_id']])[:32]
        guard.atomic(self.f.state / (key + '.json'), self.state)

    def review(self):
        return manager.review_diagnostic(guard, self.f.repo, self.state, self.options, self.checkpoint)

    def candidate(self):
        return guard.source_snapshot(self.f.repo, self.f.cfg)['content_fingerprint']

    def test_json_list_diagnostic_correction_preserves_failure_and_reports_skips(self):
        before = self.f.protected()
        failed = subprocess.run(shlex.split(query_python(self.original)), capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('AttributeError', failed.stderr)
        original_task_runs = len(self.f.task()['runs'])
        result = self.review()
        self.assertEqual(result['state'], 'reviewed')
        self.assertEqual(result['observed_objects'], 1)
        self.assertEqual(result['skipped_inputs'], [{'path': str(self.inputs / 'decisions.json'), 'type': 'list', 'object': False}])
        self.assertEqual(self.state['command_failures'], self.original_facts)
        self.assertEqual(len(self.f.task()['runs']), original_task_runs)
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, self.state, self.candidate()), {self.key})
        self.assertEqual(manager.diagnostic_resources(guard, self.state), [])
        self.assertEqual(self.f.protected(), before)
        self.core_evidence = {'original_exit_code': failed.returncode, 'original_stderr': failed.stderr,
                              'review': result, 'original_failure_preserved': self.state['command_failures'] == self.original_facts}

    def test_changed_query_wrong_guard_and_writes_cannot_be_reviewed(self):
        variants = [self.corrected.replace('isinstance(d,dict)', 'isinstance(p,dict)'),
                    self.corrected.replace('records', 'different-records'),
                    self.corrected.replace('d.get("value")', '"unrelated success"'),
                    self.corrected.replace('    print', '    Path("leak").write_text("bad")\n    print'),
                    'pnpm test:ai', 'python3 -c "import subprocess; subprocess.run([\"true\"])"']
        for text in variants:
            with self.subTest(text=text):
                with self.assertRaises(ValueError): diagnostic_correction(self.original, text)
        with self.assertRaisesRegex(ValueError, 'Assertion-bearing'):
            diagnostic_correction(self.original.replace('    print', '    assert d.get("value")==7\n    print'),
                                  self.corrected.replace('    print', '    assert d.get("value")==7\n    print'))
        self.assertFalse((self.f.repo / 'leak').exists())

    def test_all_records_skipped_is_not_success(self):
        (self.inputs / 'task.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'both valid objects'):
            self.review()
        self.assertEqual(self.state.get('diagnostic_reviews', []), [])
        self.assertEqual(self.state['command_failures'], self.original_facts)
        self.assertEqual(manager.diagnostic_resources(guard, self.state), [])

    def test_unrelated_success_and_preview_do_not_resolve_failure(self):
        self.options.apply = False
        self.assertEqual(self.review()['state'], 'planned')
        self.assertEqual(self.state.get('diagnostic_attempts', []), [])
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, self.state, self.candidate()), set())
        self.assertEqual(self.state['command_failures'], self.original_facts)

    def test_review_is_idempotent_and_bound_to_original_state_and_evidence(self):
        self.review(); self.assertFalse(self.review()['changed'])
        self.assertEqual(len(self.state['diagnostic_attempts']), 1)
        candidate = self.candidate()
        for name, value in [('session_id', 'another-session'), ('task', 'another-task'), ('policy_sha256', 'a' * 64)]:
            other = copy.deepcopy(self.state); other[name] = value
            self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, other, candidate), set())
        other = copy.deepcopy(self.state); other['command_failures'][self.key]['exit_code'] = 2
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, other, candidate), set())
        for name, value in [('failure_facts', {'exit_code': 9, 'tool_use_id': 'forged'}),
                            ('observed_objects', 99), ('observed_objects', True),
                            ('skipped_inputs', []), ('corrected_sha256', 'd' * 64)]:
            other = copy.deepcopy(self.state); other['diagnostic_reviews'][0][name] = value
            self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, other, candidate), set())
        other = copy.deepcopy(self.state)
        other['command_failures'][self.key]['exit_code'] = 9
        other['diagnostic_reviews'][0]['failure_facts'] = copy.deepcopy(other['command_failures'][self.key])
        other['diagnostic_reviews'][0]['failure_version'] = guard.digest(other['command_failures'][self.key])
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, other, candidate), set())
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, self.state, 'b' * 64), set())
        coverage = Path(self.state['diagnostic_attempts'][0]['root']) / 'evidence/coverage.json'
        coverage.write_text('{}')
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, self.state, candidate), set())

    def test_wrong_task_or_original_text_cannot_start_review(self):
        self.options.task = 'new-empty-task'
        with self.assertRaisesRegex(ValueError, 'exact repository'): self.review()
        self.options.task = self.state['task']
        self.files['original'].write_text('python3 -c "print(1)"')
        with self.assertRaisesRegex(ValueError, 'does not match'): self.review()
        self.assertEqual(self.state.get('diagnostic_attempts', []), [])

    def test_timeout_and_log_overflow_preserve_failure_and_reclaim_owned_work(self):
        with patch.object(manager, 'DIAGNOSTIC_CHILD', 'import time; time.sleep(30)'), patch.object(manager, 'DIAGNOSTIC_TIMEOUT', 0.1):
            with self.assertRaisesRegex(ValueError, 'Corrected diagnostic failed'): self.review()
        self.assertEqual(self.state['diagnostic_attempts'][-1]['exit_code'], 124)
        with patch.object(guard, 'MAX_LOG_BYTES', 8):
            with self.assertRaisesRegex(ValueError, 'Corrected diagnostic failed'): self.review()
        self.assertIn('log-coverage-unavailable', self.state['diagnostic_attempts'][-1]['violations'])
        self.assertTrue(all(x['work_reclaimed'] for x in self.state['diagnostic_attempts']))
        self.assertEqual(self.state['command_failures'], self.original_facts)
        self.assertEqual(self.state.get('diagnostic_reviews', []), [])

    def test_concurrent_source_change_or_changed_failure_rejects_review(self):
        changed = False
        def update():
            nonlocal changed
            self.checkpoint()
            if self.state.get('diagnostic_attempts') and self.state['diagnostic_attempts'][-1].get('pgid') and not changed:
                (self.f.repo / 'other.txt').write_text('concurrent task keeps this work')
                changed = True
        with self.assertRaisesRegex(ValueError, 'Corrected diagnostic failed'):
            manager.review_diagnostic(guard, self.f.repo, self.state, self.options, update)
        self.assertEqual((self.f.repo / 'other.txt').read_text(), 'concurrent task keeps this work')
        self.assertEqual(self.state.get('diagnostic_reviews', []), [])
        def change_failure():
            self.checkpoint()
            if self.state.get('diagnostic_attempts') and self.state['diagnostic_attempts'][-1].get('status') == 'complete':
                self.state['command_failures'][self.key]['exit_code'] = 4
        with self.assertRaisesRegex(ValueError, 'Original failure changed'):
            manager.review_diagnostic(guard, self.f.repo, self.state, self.options, change_failure)
        self.assertEqual(self.state.get('diagnostic_reviews', []), [])

    def test_checkpoint_interruption_reaps_owned_child_without_claiming_success(self):
        calls = 0
        def checkpoint():
            nonlocal calls
            calls += 1
            if calls == 2: raise OSError('simulated interrupted PGID checkpoint')
            self.checkpoint()
        with patch.object(manager, 'DIAGNOSTIC_CHILD', 'import time; time.sleep(30)'):
            with self.assertRaisesRegex(OSError, 'interrupted PGID'):
                manager.review_diagnostic(guard, self.f.repo, self.state, self.options, checkpoint)
        attempt = self.state['diagnostic_attempts'][-1]
        self.assertEqual(attempt['status'], 'interrupted')
        self.assertIsNotNone(attempt['exit_code'])
        self.assertTrue(attempt['work_reclaimed'])
        self.assertEqual(manager.diagnostic_resources(guard, self.state), [])
        self.assertEqual(self.state.get('diagnostic_reviews', []), [])
        self.assertEqual(self.state['command_failures'], self.original_facts)

    def test_candidate_change_before_runner_does_not_register_a_rejected_review(self):
        run = manager.run_diagnostic
        def changed(core, repo, state, request, checkpoint):
            (self.f.repo / 'other.txt').write_text('another task changed the candidate before execution')
            return run(core, repo, state, request, checkpoint)
        with patch.object(manager, 'run_diagnostic', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'candidate changed'):
                self.review()
        self.assertEqual(self.state.get('diagnostic_reviews', []), [])
        self.assertEqual(manager.reviewed_diagnostics(guard, self.f.repo, self.state, self.candidate()), set())

    def test_unknown_attempt_resources_and_other_failures_still_block_stop(self):
        self.review()
        orphan = self.f.root / 'orphan'; orphan.mkdir(); (orphan / 'work').mkdir()
        self.state['diagnostic_attempts'].append({'root': str(orphan), 'root_identity': [orphan.stat().st_dev, orphan.stat().st_ino],
            'repo': str(self.f.repo), 'task': self.state['task'], 'session_id': self.state['session_id'], 'pgid': None,
            'status': 'interrupted'})
        self.state['command_failures']['f' * 64] = {'exit_code': 3, 'tool_use_id': 'other-unresolved-command'}
        self.checkpoint()
        result = self.hooks.handle(self.f.event('Stop'))
        self.assertEqual(result.get('decision'), 'block')
        last = self.f.control()['last_audit']['unfinished']
        self.assertNotIn('command:' + self.key, last)
        self.assertIn('command:' + 'f' * 64, last)
        self.assertTrue(any(x.startswith('diagnostic-resource:') for x in last))
        self.assertTrue((orphan / 'work').exists())

    @unittest.skipUnless(ENGINE.is_file(), 'Desktop engine required')
    def test_native_legacy_diagnostic_review_allows_stop_without_erasing_failure(self):
        # Install actual definitions in this disposable home; no real trust store is used.
        (self.f.home / 'hooks.json').write_text('{"hooks":{}}')
        report = {'candidates': {str(SCRIPTS / n): installer.digest(SCRIPTS / n) for n in installer.RUNTIME_FILES},
                  'engine_version': subprocess.check_output([str(ENGINE), '--version'], text=True).strip()}
        path = self.f.root / 'accepted.json'; path.write_text(json.dumps(report))
        with patch.object(installer, 'validation', return_value=report):
            receipt = installer._install(self.f.home, self.f.repo, path, installer.digest(path))
        runtime = Path(receipt['runtime_release']); self.addCleanup(runtime.chmod, 0o700)
        self.f.policy = Path(receipt['policy']); self.f.state = self.f.home / 'task-hooks/sessions'
        before = self.f.protected(); config = (self.f.home / 'hooks.json').read_bytes()
        def review_call(_):
            current = self.f.control()
            current['command_failures'] = copy.deepcopy(self.original_facts)  # Explicit legacy migration fixture.
            key = guard.digest([str(self.f.repo), current['session_id']])[:32]
            guard.atomic(self.f.state / (key + '.json'), current)
            args = [sys.executable, '-B', str(runtime / 'manage_task_hooks.py'), '--home', str(self.f.home),
                    'review-diagnostic', '--apply', '--repo', str(self.f.repo), '--session', current['session_id'],
                    '--task', current['task'], '--failed', self.key, '--original', str(self.files['original']),
                    '--corrected', str(self.files['corrected']), '--reason', self.options.reason]
            return command(shlex.join(args))
        result = native_run(self.f.home, self.f.repo,
            [command('python3 -c "import sys; print(1)"'), review_call, message('已保留原失败并复核只读诊断。')],
            prompt='只在此临时夹具验证旧诊断复核；不提交、不跑业务测试。', sandbox='danger-full-access')
        control = self.f.control()
        self.native_evidence = [{'engine': result['engine'], 'exit_code': result['exit'], 'request_count': result['request_count'],
                                 'stdout': result['stdout'], 'stderr': result['stderr'], 'control': control}]
        self.assertEqual(result['exit'], 0, result['stderr'] + result['stdout'][-4000:])
        self.assertTrue(control['last_audit']['machine_checks_complete'], control['last_audit'])
        self.assertEqual(control['last_audit']['reviewed_diagnostic_failures'], [self.key])
        self.assertEqual(control['last_audit']['tests_auto_started'], 0)
        self.assertEqual(control['command_failures'], self.original_facts)
        self.assertEqual(control['pending_tools'], {})
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(), config)
        self.assertEqual(self.f.protected(), before)
        observed = json.loads((self.f.home / 'task-hooks/installation.json').read_text())
        self.assertEqual(observed['activation'], 'native-stop-audit-observed')
        self.assertEqual(observed['native_observation']['handler_decision'], 'allow')


class CapturedDiagnosticTest(unittest.TestCase):
    evidence = None
    repo = None

    def test_captured_actual_readonly_failure_and_corrected_inputs(self):
        if self.evidence is None: self.skipTest('Actual captured diagnostic evidence was not supplied')
        folder = Path(self.evidence); metadata = json.loads((folder / 'SOURCE.json').read_text())
        guard.no_links(folder)
        self.assertEqual(set(metadata['files']), {'original-command.txt', 'corrected-command.txt',
                                                 'original-invocation.jsonl', 'original-result.jsonl'})
        original = (folder / 'original-command.txt').read_text(); corrected = (folder / 'corrected-command.txt').read_text()
        for name, sha in metadata['files'].items():
            self.assertEqual(installer.digest(folder / name), sha)
        key = guard.digest(original); self.assertEqual(key, metadata['failure_key'])
        invocation = json.loads((folder / 'original-invocation.jsonl').read_text())['payload']
        raw_result = json.loads((folder / 'original-result.jsonl').read_text())['payload']
        self.assertEqual(invocation['call_id'], raw_result['call_id'])
        self.assertIn('AttributeError', json.dumps(raw_result))
        commands = []
        for match in re.finditer(r'\bcmd\s*:\s*(")', invocation.get('input', '')):
            value, _ = json.JSONDecoder().raw_decode(invocation['input'][match.start(1):])
            if isinstance(value, str): commands.append(value)
        self.assertIn(original, commands)
        platform = guard.no_links(Path(metadata['source_rollout']))
        self.assertTrue(platform.is_relative_to(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions'))
        with platform.open('rb') as stream:
            stream.seek(metadata['invocation_offset'])
            self.assertEqual(stream.readline(), (folder / 'original-invocation.jsonl').read_bytes())
            stream.seek(metadata['result_offset'])
            self.assertEqual(stream.readline(), (folder / 'original-result.jsonl').read_bytes())
            stream.seek(metadata['invocation_offset'])
            raw_window = stream.read(metadata['result_offset'] - metadata['invocation_offset'])
        native_items = [json.loads(line).get('payload', {}).get('item', {}) for line in raw_window.splitlines()]
        native_failure = next(item for item in native_items if isinstance(item, dict)
                              and item.get('id') == metadata['failure_facts']['tool_use_id'])
        self.assertEqual(native_failure['exit_code'], 1)
        self.assertIn('AttributeError', native_failure['aggregated_output'])
        controller_path = folder.parent.parent / 'sessions' / (guard.digest([str(self.repo), metadata['session_id']])[:32] + '.json')
        real = json.loads(controller_path.read_text())
        self.assertEqual(real['command_failures'][key], metadata['failure_facts'])
        from validate_task_hooks import protected_snapshot
        before = protected_snapshot(self.repo)
        failed = subprocess.run(shlex.split(query_python(original)), cwd=self.repo, capture_output=True, text=True, timeout=10)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('AttributeError', failed.stderr)
        state = {'repo': str(self.repo), 'session_id': 'captured-validation-state', 'task': metadata['task'],
                 'policy_sha256': 'c' * 64, 'command_failures': {key: copy.deepcopy(metadata['failure_facts'])}}
        with tempfile.TemporaryDirectory(prefix='captured-diagnostic-', dir=os.environ['CODEX_TASK_TMP_ROOT']) as tmp:
            checkpoint_path = Path(tmp) / 'review-state.json'
            options = manager.parser().parse_args(['review-diagnostic', '--apply', '--repo', str(self.repo),
                '--session', state['session_id'], '--task', state['task'], '--failed', key,
                '--original', str(folder / 'original-command.txt'), '--corrected', str(folder / 'corrected-command.txt'),
                '--reason', 'Actual metadata diagnostic failed on JSON lists; preserve output and report each guarded input'])
            try:
                result = manager.review_diagnostic(guard, self.repo, state, options, lambda: guard.atomic(checkpoint_path, state))
                attempt = state['diagnostic_attempts'][-1]
                coverage = manager.read(Path(attempt['root']) / 'evidence/coverage.json')
                self.core_evidence = {'scope': 'Captured real failure and real read-only inputs; isolated review state, not live desktop approval',
                    'original_key': key, 'original_exit_code': failed.returncode, 'original_stderr': failed.stderr,
                    'review': result, 'coverage': coverage, 'receipt_snapshot': manager.read(attempt['receipt']['path']),
                    'original_failure_preserved': state['command_failures'][key] == metadata['failure_facts']}
                self.assertTrue(result['observed_objects'])
                self.assertTrue(result['skipped_inputs'])
            finally:
                for attempt in state.get('diagnostic_attempts', []):
                    root = Path(attempt['root'])
                    self.assertIs(guard.group_active(attempt['pgid']), False)
                    self.assertEqual([root.stat().st_dev, root.stat().st_ino], attempt['root_identity'])
                    shutil.rmtree(root)
        self.assertEqual(protected_snapshot(self.repo), before)
        self.assertEqual(json.loads(controller_path.read_text())['command_failures'][key], metadata['failure_facts'])
        self.core_evidence['actual_project_and_failure_unchanged'] = True


if __name__ == '__main__': unittest.main()
