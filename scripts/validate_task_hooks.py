#!/usr/bin/env python3
"""Run local native-engine acceptance and retain its raw, version-bound results."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'scripts'))
import task_guard as guard
from hook_native_probe import ENGINE, native_run, command, message
from test_task_hooks import HookAdapterTests
from test_task_hooks_native import NativeHookTests
from test_task_guard import TaskGuardTests
from test_hook_management import ReadonlyQueries, ManagementTests, NativeQueries
from test_frozen_runtime import FrozenRuntimeTests
from test_diagnostic_review import DiagnosticReviewTests, CapturedDiagnosticTest
from test_reviewed_invocation_receipts import ReviewedInvocationTests


def protected_snapshot(repo):
    facts = {}
    for tree in guard.worktrees(repo):
        root = Path(tree['worktree'])
        if not root.is_dir():
            continue
        raw = guard.git(root, 'status', '--porcelain=v1', '-z', '-uall').stdout
        index = Path(guard.value(root, 'rev-parse', '--git-path', 'index'))
        index = index if index.is_absolute() else root / index
        changed = {}
        rows = raw.split(b'\0'); i = 0
        while i < len(rows):
            row = rows[i]; i += 1
            if row:
                name = os.fsdecode(row[3:]); changed[name] = guard.signature(root / name)
                if b'R' in row[:2] or b'C' in row[:2]:
                    i += 1
        facts[str(root)] = {'status_hex': raw.hex(), 'index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
                            'head': guard.value(root, 'rev-parse', 'HEAD'), 'files': changed}
    return facts


class LiveFoundationTest(unittest.TestCase):
    repo = None

    def test_live_foundation_automatic_light_check(self):
        repo = guard.root_of(self.repo)
        before = protected_snapshot(repo)
        marker = 'native-auto-check-' + uuid.uuid4().hex[:12]
        parent = repo / '.runtime/test-workspaces'
        parent_existed = parent.exists()
        probe = parent / marker
        task_file = None
        with tempfile.TemporaryDirectory(prefix='hook-live-', dir='/private/tmp') as folder:
            root = Path(folder).resolve(); home = root / 'home'; home.mkdir()
            policy = root / 'policy.json'; state_root = root / 'state'
            policy.write_text(json.dumps({'schema_version': 1, 'scope': 'pilot', 'pilot_roots': [str(repo)],
                                          'heavy_tests': ['ai'], 'pinned_files': [], 'protected_files': []}))
            handler = shlex.join([sys.executable, '-B', str(ROOT / 'scripts/task_hooks.py'),
                                  '--policy', str(policy), '--state-root', str(state_root)])
            events = ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'Interrupt']
            (home / 'hooks.json').write_text(json.dumps({'hooks': {
                event: [{'hooks': [{'type': 'command', 'command': handler, 'timeout': 3 if event == 'Interrupt' else 20}]}]
                for event in events}}))
            write = 'from pathlib import Path;p=Path(' + repr(str(probe)) + ');p.mkdir(parents=True);(p/"copy.md").write_text("isolated hook probe\\n")'
            clean = 'from pathlib import Path;p=Path(' + repr(str(probe)) + ');(p/"copy.md").unlink();p.rmdir()'
            try:
                result = native_run(home, repo, [command(shlex.join([sys.executable, '-c', write])), message('完成。'),
                                                command(shlex.join([sys.executable, '-c', clean])), message('本轮测试文件已回收。')],
                                    '仅在本轮指定测试目录写入并回收文案夹具；不提交、不运行项目测试、不触碰其他任务。')
                self.assertEqual(result['exit'], 0, result['stderr'])
                records = [guard.regular_json(p) for p in state_root.glob('*.json')]
                self.assertEqual(len(records), 1)
                control = records[0]
                task_file = guard.common_of(repo) / 'codex-workflow' / (control['task'] + '.json')
                raw_task = task_file.read_bytes()
                self.native_evidence = [{'engine': result['engine'], 'exit_code': result['exit'],
                                         'request_count': result['request_count'], 'stdout': result['stdout'],
                                         'stderr': result['stderr'], 'control': control,
                                         'task_record': raw_task.decode(), 'task_record_sha256': hashlib.sha256(raw_task).hexdigest(),
                                         'repo': str(repo)}]
                self.assertTrue(control['last_audit']['machine_checks_complete'])
                self.assertEqual(control['baseline_captures'], 1)
                self.assertEqual(control['full_audits'], 2)
                self.assertGreaterEqual(result['request_count'], 4)
                self.assertEqual(guard.regular_json(task_file)['runs'], [])
                self.assertFalse(probe.exists())
                self.assertEqual(before, protected_snapshot(repo))
            finally:
                # Only this unique fixture path may be removed. Unknown additions
                # or identity changes stop cleanup rather than widening deletion.
                if probe.exists():
                    guard.no_links(probe)
                    names = {p.name for p in probe.iterdir()}
                    self.assertTrue(names <= {'copy.md'})
                    if (probe / 'copy.md').exists():
                        self.assertEqual((probe / 'copy.md').read_text(), 'isolated hook probe\n')
                        (probe / 'copy.md').unlink()
                    probe.rmdir()
                if task_file and task_file.exists():
                    task = guard.regular_json(task_file)
                    self.assertEqual(task['repo'], str(repo))
                    self.assertEqual(task['task'], control['task'])
                    # The exact original record is retained in native_evidence.
                    task_file.unlink()
                if not parent_existed and parent.is_dir():
                    try:
                        parent.rmdir()
                    except OSError:
                        pass  # Concurrent or pre-existing resources are preserved.
        self.assertEqual(before, protected_snapshot(repo))


def candidates():
    return [ROOT / 'scripts/task_hooks.py', ROOT / 'scripts/task_guard.py', ROOT / 'scripts/closeout_progress.py',
            ROOT / 'tests/test_task_hooks.py', ROOT / 'tests/test_task_hooks_native.py',
            ROOT / 'tests/hook_native_probe.py', ROOT / 'tests/test_task_guard.py', ROOT / 'scripts/readonly_query.py',
            ROOT / 'scripts/manage_task_hooks.py', ROOT / 'scripts/install_task_hooks.py',
            ROOT / 'tests/test_hook_management.py', ROOT / 'tests/test_frozen_runtime.py',
            ROOT / 'tests/test_diagnostic_review.py', ROOT / 'tests/test_reviewed_invocation_receipts.py', Path(__file__).resolve()]


def fingerprints():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in candidates()}


def validate(repo, output, diagnostic_evidence=None):
    initial = fingerprints()
    captured_files = {}
    if diagnostic_evidence:
        folder = guard.no_links(Path(diagnostic_evidence).absolute())
        for name in ['SOURCE.json', 'original-command.txt', 'corrected-command.txt', 'original-invocation.jsonl', 'original-result.jsonl']:
            file = guard.no_links(folder / name)
            captured_files[str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
    class Result(unittest.TextTestResult):
        cases = []
        def addSuccess(self, test):
            super().addSuccess(test)
            self.cases.append({'name': test.id(), 'status': 'passed', 'native': getattr(test, 'native_evidence', []),
                               'core': getattr(test, 'core_evidence', None)})
        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.cases.append({'name': test.id(), 'status': 'failed', 'native': getattr(test, 'native_evidence', [])})
        def addError(self, test, err):
            super().addError(test, err)
            self.cases.append({'name': test.id(), 'status': 'error', 'native': getattr(test, 'native_evidence', [])})
    LiveFoundationTest.repo = str(repo)
    CapturedDiagnosticTest.repo = repo
    CapturedDiagnosticTest.evidence = diagnostic_evidence
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite([loader.loadTestsFromTestCase(HookAdapterTests),
                               loader.loadTestsFromTestCase(NativeHookTests),
                               loader.loadTestsFromTestCase(LiveFoundationTest),
                               loader.loadTestsFromTestCase(ReadonlyQueries),
                               loader.loadTestsFromTestCase(ManagementTests),
                               loader.loadTestsFromTestCase(NativeQueries),
                               loader.loadTestsFromTestCase(FrozenRuntimeTests),
                               loader.loadTestsFromTestCase(DiagnosticReviewTests),
                               loader.loadTestsFromTestCase(CapturedDiagnosticTest),
                               loader.loadTestsFromTestCase(ReviewedInvocationTests),
                               TaskGuardTests('test_unknown_process_group_never_completes_reclaimed_interruption')])
    outcome = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    captured_stable = all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in captured_files.items())
    report = {'format': 'codex-task-hooks-validation-v1', 'repo': str(repo), 'engine': str(ENGINE),
              'engine_version': subprocess.check_output([str(ENGINE), '--version'], text=True).strip(),
              'created_at': time.time(), 'candidates': initial, 'candidate_stable': initial == fingerprints() and captured_stable,
              'diagnostic_evidence': captured_files,
              'tests_run': outcome.testsRun, 'cases': outcome.cases,
              'exit_code': 0 if outcome.wasSuccessful() and initial == fingerprints() and captured_stable else 1,
              'scope': 'Isolated local provider and actual desktop-bundled engine; no paid API or production use. Hook trust bypass is confined to these vetted temporary fixtures, never global configuration.'}
    guard.atomic(Path(output), report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True); parser.add_argument('--output', required=True)
    parser.add_argument('--diagnostic-evidence')
    args = parser.parse_args()
    result = validate(guard.root_of(args.repo), Path(args.output).absolute(), args.diagnostic_evidence)
    print(json.dumps({'exit_code': result['exit_code'], 'tests_run': result['tests_run'], 'report': str(Path(args.output).absolute())}))
    raise SystemExit(result['exit_code'])
