"""A native-backed empty ps query is an observation, never a passing business test."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_task_hooks import HookFixture, SCRIPTS
from hook_native_probe import ENGINE, native_run, command, message
from task_hooks import Hooks
import task_guard as guard
import manage_task_hooks as manager
import install_task_hooks as installer
from readonly_query import diagnostic_correction, readonly_command, process_query


def native_evidence(folder, home, repo, session, key, text, facts):
    """Explicit legacy fixture; real production evidence is covered separately below."""
    marker = '__CODEX_TASK_EXIT_' + 'a' * 32 + '__'
    wrapped = '(\n' + text + '\n)\ncodex_task_exit=$?\nprintf "\\n' + marker + ':%s\\n" "$codex_task_exit"\nexit "$codex_task_exit"'
    event = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': session,
        'item': {'type': 'CommandExecution', 'id': facts['tool_use_id'], 'status': 'failed', 'exit_code': 1,
        'cwd': repo.as_uri(), 'command': ['/bin/zsh', '-lc', wrapped],
        'aggregated_output': '  PID  PPID  PGID ELAPSED COMMAND\n\n' + marker + ':1\n'}}}
    journal = home / 'sessions/rollout-process-legacy.jsonl'; journal.parent.mkdir(exist_ok=True)
    raw = (json.dumps(event) + '\n').encode(); journal.write_bytes(raw)
    result = folder / 'native-result.jsonl'; result.write_bytes(raw)
    source = folder / 'native-source.json'
    source.write_text(json.dumps({'format': 'native-process-query-source-v1', 'source': str(journal),
                                  'offset': 0, 'result_sha256': installer.digest(result)}))
    return source, result, journal


class ProcessReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture(); self.addCleanup(self.f.close)
        self.f.policy = self.f.home / 'task-hooks/policies/fixture.json'; self.f.policy.parent.mkdir(parents=True)
        self.f.write_policy(); self.f.install_definition()
        self.hooks = Hooks(self.f.policy, self.f.state)
        self.hooks.handle(self.f.event('PreToolUse', 'python3 -c "import sys; print(1)"'))
        self.hooks.handle(self.f.event('PostToolUse'))
        self.state = self.f.control()
        # Pick a supported, currently empty PID rather than using an out-of-range ID.
        self.pid = None
        for pid in range(99998, 99990, -1):
            probe = subprocess.run(['/bin/ps', '-o', 'pid,ppid,pgid,etime,command', '-p', str(pid)], capture_output=True, text=True)
            if probe.returncode == 1 and not probe.stderr and probe.stdout.split() == ['PID', 'PPID', 'PGID', 'ELAPSED', 'COMMAND']:
                self.pid = pid; break
        self.assertIsNotNone(self.pid, 'No empty supported fixture PID available')
        self.text = 'ps -o pid,ppid,pgid,etime,command -p ' + str(self.pid)
        self.key = guard.digest(self.text)
        self.state['command_failures'] = {self.key: {'exit_code': 1, 'tool_use_id': 'legacy-ps-query'}}
        self.original_facts = copy.deepcopy(self.state['command_failures'])
        self.original = self.f.root / 'original-ps.txt'; self.original.write_text(self.text)
        self.source, self.result, self.journal = native_evidence(self.f.root, self.f.home, self.f.repo,
            self.state['session_id'], self.key, self.text, self.state['command_failures'][self.key])
        self.origins = {self.key: manager.read(self.source)}
        self.options = manager.parser().parse_args(['--home', str(self.f.home), 'review-diagnostic', '--apply',
            '--repo', str(self.f.repo), '--session', self.state['session_id'], '--task', self.state['task'],
            '--failed', self.key, '--original', str(self.original), '--corrected', str(self.original),
            '--native-source', str(self.source), '--native-result', str(self.result),
            '--reason', 'Original exact native ps result was empty; retain exit one and observe the same numeric PID again'])
        self.checkpoint()

    def checkpoint(self):
        path = self.f.state / (guard.digest([str(self.f.repo), self.state['session_id']])[:32] + '.json')
        guard.atomic(path, self.state)

    def candidate(self):
        return guard.source_snapshot(self.f.repo, self.f.cfg)['content_fingerprint']

    def accepted(self, state=None):
        return manager.reviewed_diagnostics(guard, self.f.repo, state or self.state, self.candidate(), self.f.home, self.origins)

    def review(self):
        return manager.review_diagnostic(guard, self.f.repo, self.state, self.options, self.checkpoint, self.origins)

    def test_empty_ps_query_retains_exit_one_and_original_failure(self):
        before = self.f.protected(); runs = copy.deepcopy(self.f.task()['runs'])
        result = self.review(); attempt = self.state['diagnostic_attempts'][-1]
        self.assertEqual(result['outcome'], 'no-matching-process'); self.assertEqual(result['query_exit'], 1)
        self.assertEqual(attempt['exit_code'], 1)
        self.assertEqual(attempt['execution_argv'], ['/bin/ps', '-o', 'pid,ppid,pgid,etime,command', '-p', str(self.pid)])
        self.assertEqual(self.accepted(), {self.key}); self.assertEqual(self.state['command_failures'], self.original_facts)
        self.assertEqual(manager.diagnostic_resources(guard, self.state), [])
        self.assertEqual(self.f.protected(), before); self.assertEqual(self.f.task()['runs'], runs)
        self.assertFalse(self.review()['changed'])

    def test_current_matching_pid_does_not_claim_original_process_identity(self):
        self.text = 'ps -o pid,ppid,pgid,etime,command -p ' + str(os.getpid())
        self.original.write_text(self.text); self.key = guard.digest(self.text)
        self.options.failed = self.key; self.state['command_failures'] = {self.key: {'exit_code': 1, 'tool_use_id': 'legacy-ps-query'}}
        self.original_facts = copy.deepcopy(self.state['command_failures'])
        native_evidence(self.f.root, self.f.home, self.f.repo, self.state['session_id'], self.key, self.text, self.state['command_failures'][self.key])
        self.origins = {self.key: manager.read(self.source)}
        result = self.review()
        self.assertEqual(result['query_exit'], 0); self.assertEqual(result['outcome'], 'matching-process-now')
        self.assertEqual(result['observed_processes'][0][0], str(os.getpid()))
        self.assertEqual(self.accepted(), {self.key}); self.assertEqual(self.state['command_failures'], self.original_facts)

    def test_ps_arguments_writes_and_changed_query_are_not_reviewable(self):
        for text in [self.text + '; true', self.text + ' | cat', self.text + ' --help',
                     self.text.replace('-p '+str(self.pid), '-p 1,2'), self.text.replace('ps ', 'sudo ps '),
                     self.text.replace(str(self.pid), '0'), self.text.replace(str(self.pid), '9999999999')]:
            with self.subTest(text=text):
                self.assertIsNone(process_query(text))
                with self.assertRaises(ValueError): diagnostic_correction(text, text)
        with self.assertRaises(ValueError): diagnostic_correction(self.text, self.text.replace(str(self.pid), '1'))
        self.assertEqual(readonly_command(self.text), '/bin/' + self.text)
        with patch('readonly_query.shutil.which', return_value='/untrusted/ps'):
            self.assertIsNone(process_query(self.text)); self.assertIsNone(readonly_command(self.text))

    def test_wrong_native_identity_output_marker_or_journal_never_accepts(self):
        original = self.result.read_bytes(); metadata = self.source.read_bytes()
        for field, value in [('thread_id', 'other-session'), ('id', 'other-tool'), ('cwd', 'file:///wrong'),
                             ('exit_code', True), ('command', ['/bin/zsh', '-lc', self.text]),
                             ('aggregated_output', '  PID  PPID  PGID ELAPSED COMMAND\npermission denied\n')]:
            event = json.loads(original)
            target = event['payload'] if field == 'thread_id' else event['payload']['item']; target[field] = value
            raw = (json.dumps(event)+'\n').encode(); self.result.write_bytes(raw); self.journal.write_bytes(raw)
            meta = json.loads(metadata); meta['result_sha256'] = installer.digest(self.result); self.source.write_text(json.dumps(meta))
            with self.subTest(field=field), patch.dict(self.origins, {self.key: meta}, clear=True), self.assertRaises(ValueError): self.review()
            self.assertEqual(self.state.get('diagnostic_attempts', []), [])
        self.result.write_bytes(original); self.journal.write_bytes(original)
        self.source.write_bytes(metadata)
        meta = json.loads(metadata); meta['offset'] = 1; self.source.write_text(json.dumps(meta))
        with patch.dict(self.origins, {self.key: meta}, clear=True), self.assertRaises(ValueError): self.review()
        meta = json.loads(metadata); meta['offset'] = 10**100; self.source.write_text(json.dumps(meta))
        with patch.dict(self.origins, {self.key: meta}, clear=True), self.assertRaises(ValueError): self.review()
        meta = json.loads(metadata); outside = self.f.root/'rollout-fake.jsonl'; outside.write_bytes(original); meta['source'] = str(outside)
        self.source.write_text(json.dumps(meta))
        with self.assertRaises(ValueError): self.review()
        self.source.write_bytes(metadata)
        fake = self.f.home/'sessions/rollout-forged.jsonl'; fake.write_bytes(original)
        meta = json.loads(metadata); meta['source'] = str(fake); self.source.write_text(json.dumps(meta))
        with self.assertRaisesRegex(ValueError, 'not pinned'): self.review()
        self.source.write_text('['*2000+'0'+']'*2000)
        with self.assertRaisesRegex(ValueError, 'nested too deeply'): self.review()
        self.assertEqual(self.state['command_failures'], self.original_facts)

    def test_bad_ps_results_and_unknown_resources_do_not_get_review(self):
        result_dir = self.f.root/'table'; result_dir.mkdir(); (result_dir/'stderr.log').write_text('')
        header = '  PID  PPID  PGID ELAPSED COMMAND\n'
        for code, out, err in [(0, header, ''), (1, header+'1 0 1 00:00 init\n', ''),
                               (1, header, 'permission denied'), (2, header, ''),
                               (0, header+'12 1 12 00:01 wrong pid\n', '')]:
            (result_dir/'stdout.log').write_text(out); (result_dir/'stderr.log').write_text(err)
            with self.assertRaises(ValueError): manager.process_coverage(result_dir, process_query(self.text), code)
        popen = manager.subprocess.Popen
        def unavailable(argv, *args, **kwargs):
            if argv[0] == '/bin/ps': raise OSError('ps cannot start')
            return popen(argv, *args, **kwargs)
        with patch.object(manager.subprocess, 'Popen', side_effect=unavailable):
            with self.assertRaises(OSError): self.review()
        self.assertEqual(self.accepted(), set()); self.assertTrue(manager.diagnostic_resources(guard, self.state))
        self.assertEqual(self.state['command_failures'], self.original_facts)
        # No child was created; explicitly reap only this fixture's registered empty work in teardown.
        attempt = self.state['diagnostic_attempts'][-1]; root = Path(attempt['root'])
        self.assertIsNone(attempt['pgid']); guard.remove_run_work(attempt); shutil.rmtree(root)

    def test_ps_review_tampering_policy_or_unrelated_failure_remains_blocking(self):
        self.review(); candidate = self.candidate()
        for field, value in [('policy_sha256', 'f'*64), ('task', 'another-task'), ('session_id', 'another-session')]:
            other = copy.deepcopy(self.state); other[field] = value
            self.assertEqual(self.accepted(other), set())
        for field, value in [('query_exit', 0), ('query_exit', True), ('observed_processes', [['unrelated']]),
                             ('execution_argv', ['/bin/true']), ('native_home', str(self.f.root))]:
            other = copy.deepcopy(self.state); other['diagnostic_reviews'][0][field] = value
            self.assertEqual(self.accepted(other), set())
        attempt = self.state['diagnostic_attempts'][0]
        other = copy.deepcopy(self.state); other['diagnostic_attempts'][0]['pgid'] = None
        self.assertEqual(self.accepted(other), set()); self.assertTrue(manager.diagnostic_resources(guard, other))
        native = Path(attempt['root'])/'evidence/native-result.jsonl'; native.chmod(0o600); native.write_text('{}')
        self.assertEqual(self.accepted(), set())
        self.assertEqual(self.state['command_failures'], self.original_facts)

    def test_ps_timeout_retains_failure_and_reaps_only_owned_child(self):
        popen = manager.subprocess.Popen
        def hung(argv, *args, **kwargs):
            if argv[0] == '/bin/ps':
                return popen([sys.executable, '-c', 'import time; time.sleep(30)'], *args, **kwargs)
            return popen(argv, *args, **kwargs)
        with patch.object(manager.subprocess, 'Popen', side_effect=hung), patch.object(manager, 'DIAGNOSTIC_TIMEOUT', 0.05):
            with self.assertRaisesRegex(ValueError, 'Corrected diagnostic failed'): self.review()
        attempt = self.state['diagnostic_attempts'][-1]
        self.assertEqual(attempt['exit_code'], 124); self.assertTrue(attempt['work_reclaimed'])
        self.assertEqual(manager.diagnostic_resources(guard, self.state), [])
        self.assertEqual(self.accepted(), set()); self.assertEqual(self.state['command_failures'], self.original_facts)

    @unittest.skipUnless(ENGINE.is_file(), 'Desktop engine required')
    def test_native_ps_review_preserves_exit_one_and_blocks_other_failure(self):
        (self.f.home/'hooks.json').write_text('{"hooks":{}}')
        # Discover a real isolated engine session, then pin this explicit legacy
        # fixture before resuming that same session. The model cannot choose the pin.
        seed = native_run(self.f.home, self.f.repo, [message('Prepare isolated legacy migration fixture.')],
                          sandbox='danger-full-access', persist=True)
        self.assertEqual(seed['exit'], 0, seed['stderr'])
        session = next(json.loads(line)['thread_id'] for line in seed['stdout'].splitlines()
                       if json.loads(line).get('type') == 'thread.started')
        native_evidence(self.f.root, self.f.home, self.f.repo, session, self.key, self.text, self.original_facts[self.key])
        report = {'candidates': {str(SCRIPTS/n): installer.digest(SCRIPTS/n) for n in installer.RUNTIME_FILES},
                  'process_origins': {self.key: manager.read(self.source)},
                  'engine_version': subprocess.check_output([str(ENGINE), '--version'], text=True).strip()}
        path = self.f.root/'accepted.json'; path.write_text(json.dumps(report))
        with patch.object(installer, 'validation', return_value=report):
            receipt = installer._install(self.f.home, self.f.repo, path, installer.digest(path))
        runtime = Path(receipt['runtime_release']); self.addCleanup(runtime.chmod, 0o700)
        self.f.policy = Path(receipt['policy']); self.f.state = self.f.home/'task-hooks/sessions'
        before = self.f.protected()
        def review_call(_):
            current = self.f.control(); current['command_failures'] = copy.deepcopy(self.original_facts)
            self.assertEqual(current['session_id'], session)
            current['command_failures']['f'*64] = {'exit_code': 9, 'tool_use_id': 'unrelated-business-failure'}
            guard.atomic(self.f.state/(guard.digest([str(self.f.repo), current['session_id']])[:32]+'.json'), current)
            argv = [sys.executable, '-B', str(runtime/'manage_task_hooks.py'), '--home', str(self.f.home),
                'review-diagnostic', '--apply', '--repo', str(self.f.repo), '--session', current['session_id'],
                '--task', current['task'], '--failed', self.key, '--original', str(self.original), '--corrected', str(self.original),
                '--native-source', str(self.source), '--native-result', str(self.result), '--reason', self.options.reason]
            return command(shlex.join(argv))
        result = native_run(self.f.home, self.f.repo,
            [command('python3 -c "import sys; print(1)"'), review_call, message('部分完成，其他失败仍保留。'), message('部分完成，其他失败仍保留。'), message('部分完成，其他失败仍保留。')],
            prompt='Only this isolated native migration fixture; no project test suite.', sandbox='danger-full-access', resume=session)
        control = self.f.control()
        self.native_evidence = [{'engine': result['engine'], 'exit_code': result['exit'], 'request_count': result['request_count'],
            'stdout': result['stdout'], 'stderr': result['stderr'], 'control': control}]
        self.assertEqual(result['exit'], 0, result['stderr']+result['stdout'][-3000:])
        self.assertEqual(control['last_audit']['reviewed_diagnostic_failures'], [self.key])
        self.assertEqual(control['last_audit']['unfinished'], ['command:'+'f'*64])
        self.assertFalse(control['last_audit']['machine_checks_complete'])
        self.assertEqual(control['diagnostic_attempts'][0]['exit_code'], 1)
        self.assertEqual(control['command_failures'][self.key], self.original_facts[self.key])
        self.assertEqual(control['last_audit']['tests_auto_started'], 0); self.assertEqual(self.f.protected(), before)


class CapturedProcessReviewTest(unittest.TestCase):
    evidence = None
    repo = None

    def test_captured_actual_83_ps_query_preserves_native_failure(self):
        if self.evidence is None: self.skipTest('Actual process query evidence required')
        folder = guard.no_links(Path(self.evidence)); original = (folder/'original-command.txt').read_text()
        metadata = manager.read(folder/'SOURCE.json')
        for name, sha in metadata['files'].items(): self.assertEqual(installer.digest(folder/name), sha)
        key = guard.digest(original); self.assertEqual(key, metadata['command_key'])
        home = folder.parent.parent.parent
        state_path = home/'task-hooks/sessions'/(guard.digest([str(self.repo), '01a1c749-f3b5-7891-9515-d0a18b821cf8'])[:32]+'.json')
        actual = guard.regular_json(state_path); failure = copy.deepcopy(actual['command_failures'][key])
        state = {'repo': str(self.repo), 'session_id': actual['session_id'], 'task': actual['task'],
                 'policy_sha256': 'c'*64, 'command_failures': {key: failure}}
        options = manager.parser().parse_args(['--home', str(home), 'review-diagnostic', '--apply', '--repo', str(self.repo),
            '--session', state['session_id'], '--task', state['task'], '--failed', key,
            '--original', str(folder/'original-command.txt'), '--corrected', str(folder/'original-command.txt'),
            '--native-source', str(folder/'native-source.json'), '--native-result', str(folder/'original-result.jsonl'),
            '--reason', 'Exact original empty process table was a read-only query; preserve exit one and repeat only this PID'])
        from validate_task_hooks import protected_snapshot
        origins = {key: manager.read(folder/'native-source.json')}
        before = protected_snapshot(self.repo); before_failure = copy.deepcopy(actual['command_failures'])
        with tempfile.TemporaryDirectory(prefix='captured-ps-', dir=os.environ['CODEX_TASK_TMP_ROOT']) as tmp:
            try:
                result = manager.review_diagnostic(guard, self.repo, state, options, lambda: guard.atomic(Path(tmp)/'state.json', state), origins)
                attempt = state['diagnostic_attempts'][-1]
                self.assertEqual(manager.reviewed_diagnostics(guard, self.repo, state, manager.read(attempt['receipt']['path'])['candidate'], home, origins), {key})
                self.assertEqual(state['command_failures'][key], failure)
                self.core_evidence = {'scope': 'Actual captured native record; isolated review controller, not live desktop approval',
                    'original_key': key, 'original_exit_code': failure['exit_code'], 'original_native_id': failure['tool_use_id'],
                    'review': result, 'execution_argv': attempt['execution_argv'], 'rerun_exit_code': attempt['exit_code'],
                    'original_failure_preserved': True, 'resources_closed': manager.diagnostic_resources(guard, state) == []}
            finally:
                for attempt in state.get('diagnostic_attempts', []):
                    self.assertIs(guard.run_group_active(attempt), False)
                    root = Path(attempt['root']); self.assertEqual([root.stat().st_dev, root.stat().st_ino], attempt['root_identity']); shutil.rmtree(root)
        self.assertEqual(guard.regular_json(state_path)['command_failures'], before_failure)
        self.assertEqual(protected_snapshot(self.repo), before)
        self.core_evidence['actual_project_and_failure_unchanged'] = True


if __name__ == '__main__': unittest.main()
