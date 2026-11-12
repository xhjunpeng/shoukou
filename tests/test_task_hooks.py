"""Behavior and cost controls for the synchronous hook adapter."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import task_guard as guard
from task_hooks import Hooks, EVENTS, is_readonly, closeout_requirements


class HookFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-workflow-', dir='/private/tmp')
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.home = self.root / 'home'; self.home.mkdir()
        self.state = self.root / 'state'
        self.policy = self.root / 'policy.json'
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Isolated fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', str(self.root / 'no-git-hooks'))
        (self.repo / '.codex').mkdir()
        (self.repo / '.gitignore').write_text('.runtime/\nheavy-ran\n')
        (self.repo / 'README.md').write_text('original\n')
        (self.repo / 'other.txt').write_text('other task original\n')
        (self.repo / 'focused.py').write_text('from pathlib import Path\nassert Path("logic.py").read_text()=="value = 2\\n"\nprint("FOCUSED_PASSED")\n')
        (self.repo / 'heavy.py').write_text('from pathlib import Path\nPath("heavy-ran").write_text("unexpected")\nraise SystemExit(9)\n')
        self.cfg = {'schema_version': 1, 'main_branch': 'main', 'remote': 'origin',
                    'output_roots': ['.runtime/private-resources', '.runtime/test-workspaces'],
                    'tests': {'focused': [sys.executable, 'focused.py'], 'ai': [sys.executable, 'heavy.py']}}
        (self.repo / '.codex/workflow.json').write_text(json.dumps(self.cfg))
        self.git('add', '.'); self.git('commit', '-m', 'fixture baseline')
        self.write_policy()
        self.install_definition()

    def git(self, *args, repo=None):
        return subprocess.check_output(['git', '--no-optional-locks', '-C', str(repo or self.repo), *args], stderr=subprocess.DEVNULL)

    def write_policy(self, **extra):
        obj = {'schema_version': 1, 'scope': 'pilot', 'pilot_roots': [str(self.repo)],
               'heavy_tests': ['ai'], 'pinned_files': [], 'protected_files': [str(self.home / 'hooks.json')]}
        obj.update(extra)
        self.policy.write_text(json.dumps(obj))

    def install_definition(self):
        import shlex
        cmd = shlex.join([sys.executable, '-B', str(SCRIPTS / 'task_hooks.py'),
                          '--policy', str(self.policy), '--state-root', str(self.state)])
        definition = {'description': 'Isolated workflow acceptance only', 'hooks': {
            event: [{'hooks': [{'type': 'command', 'command': cmd, 'timeout': 3 if event == 'Interrupt' else 15}]}]
            for event in EVENTS}}
        (self.home / 'hooks.json').write_text(json.dumps(definition))

    def event(self, name, command=None, tool='Bash', session='fixture-session', **extra):
        obj = {'session_id': session, 'turn_id': 'fixture-turn', 'cwd': str(self.repo),
               'hook_event_name': name, 'tool_name': tool, 'tool_use_id': 'call-fixture'}
        if command is not None:
            obj['tool_input'] = {'command': command}
        if name == 'PostToolUse' and 'tool_response' not in extra and self.state.exists():
            current = self.control(session)
            pending = current.get('pending_tools', {}).get('call-fixture', {})
            if pending.get('exit_marker'):
                obj['tool_response'] = pending['exit_marker'] + ':0\n'
        obj.update(extra)
        return obj

    def control(self, session=None):
        files = list(self.state.glob('*.json'))
        records = [json.loads(p.read_text()) for p in files]
        return next(r for r in records if session is None or r['session_id'] == session)

    def task(self, session=None):
        control = self.control(session)
        return json.loads((guard.common_of(self.repo) / 'codex-workflow' / (control['task'] + '.json')).read_text())

    def protected(self):
        raw = self.git('status', '--porcelain=v1', '-z', '-uall')
        return {'index': hashlib.sha256((self.repo / '.git/index').read_bytes()).hexdigest(),
                'staged': self.git('ls-files', '--stage', '-z'), 'cached': self.git('diff', '--cached', '--binary'),
                'status': raw, 'other': (self.repo / 'other.txt').read_bytes()}

    def close(self):
        # These exact run directories were made by fixtures in this temporary
        # Git repository. Never enumerate or remove other /tmp batches.
        folder = guard.common_of(self.repo) / 'codex-workflow'
        if folder.exists():
            for f in folder.glob('*.json'):
                record = json.loads(f.read_text())
                for run in record.get('runs', []):
                    root = Path(run['root'])
                    if root.exists():
                        assert root.name.startswith('codex-task-guard-')
                        assert [root.stat().st_dev, root.stat().st_ino] == run['root_identity']
                        assert guard.group_active(run.get('pgid')) is False
                        shutil.rmtree(root)
        self.temp.cleanup()
        assert not self.root.exists()


class HookAdapterTests(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture(); self.addCleanup(self.f.close)
        self.h = Hooks(self.f.policy, self.f.state)

    def test_readonly_question_creates_no_development_baseline(self):
        self.h.handle(self.f.event('UserPromptSubmit', prompt='现在是什么情况？'))
        self.h.handle(self.f.event('PreToolUse', 'git status --short'))
        self.h.handle(self.f.event('Stop'))
        self.assertFalse(self.f.control()['task_started'])
        self.assertFalse((self.f.repo / '.git/codex-workflow').exists())

    def test_light_edits_capture_once_and_never_call_full_scan_per_operation(self):
        self.h.handle(self.f.event('UserPromptSubmit', prompt='修改一句文案，先不要提交'))
        patch_text = '*** Begin Patch\n*** Update File: README.md\n@@\n-original\n+new\n*** End Patch'
        self.h.handle(self.f.event('PreToolUse', patch_text, tool='apply_patch'))
        self.h.handle(self.f.event('PostToolUse', patch_text, tool='apply_patch'))
        with patch.object(guard, 'observation', side_effect=AssertionError('per-operation full scan')):
            for _ in range(20):
                self.h.handle(self.f.event('PreToolUse', patch_text, tool='apply_patch'))
                self.h.handle(self.f.event('PostToolUse', patch_text, tool='apply_patch'))
        self.assertEqual(self.f.control()['baseline_captures'], 1)
        self.assertEqual(self.f.control()['full_audits'], 0)
        self.assertFalse((self.f.repo / 'heavy-ran').exists())

    def test_existing_staged_unstaged_and_untracked_are_not_overwritten(self):
        (self.f.repo / 'other.txt').write_text('staged\n'); self.f.git('add', 'other.txt')
        (self.f.repo / 'other.txt').write_text('unstaged\n')
        (self.f.repo / 'unique.txt').write_text('untracked\n')
        before = self.f.protected()
        result = self.h.handle(self.f.event('PreToolUse', '*** Update File: other.txt', tool='apply_patch'))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(before, self.f.protected())

    def test_patch_traversal_and_control_changes_are_denied(self):
        for path in [self.f.repo / '../foreign.txt', self.f.home / 'hooks.json']:
            r = self.h.handle(self.f.event('PreToolUse', '*** Update File: ' + str(path), tool='apply_patch'))
            self.assertEqual(r['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_failed_integrity_is_explicit_deny_through_cli(self):
        self.f.write_policy(pinned_files=[{'path': str(SCRIPTS / 'task_guard.py'), 'sha256': '0' * 64}])
        r = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'task_hooks.py'), '--policy', str(self.f.policy),
                            '--state-root', str(self.f.state)], input=json.dumps(self.f.event('PreToolUse', 'echo x')),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_real_stop_detects_omitted_output_without_starting_tests(self):
        self.h.handle(self.f.event('PreToolUse', 'mkdir fixture'))
        p = self.f.repo / '.runtime/private-resources/materials-omitted/files'
        p.mkdir(parents=True)
        for i, size in enumerate([75, 95, 90, 48]):
            (p / str(i)).write_bytes(b'x' * size)
        self.h.handle(self.f.event('PostToolUse', 'mkdir fixture'))
        result = self.h.handle(self.f.event('Stop'))
        self.assertEqual(result['decision'], 'block')
        self.assertEqual(len(self.f.control()['last_audit']['unfinished']), 4)
        self.assertEqual(self.f.task()['runs'], [])
        shutil.rmtree(self.f.repo / '.runtime')
        self.assertEqual(self.h.handle(self.f.event('Stop')), {})

    def test_pause_does_not_force_continuation_or_clean_other_data(self):
        self.h.handle(self.f.event('PreToolUse', 'echo mutation'))
        self.h.handle(self.f.event('PostToolUse', 'echo mutation'))
        self.h.handle(self.f.event('UserPromptSubmit', prompt='暂停，不要继续执行'))
        self.assertEqual(self.h.handle(self.f.event('Stop')), {})

    def test_pending_command_blocks_stop_before_first_completed_mutation(self):
        self.h.handle(self.f.event('UserPromptSubmit', prompt='运行本轮命令，不要提交'))
        self.h.handle(self.f.event('PreToolUse', 'python3 -c "import time;time.sleep(5)"'))
        result = self.h.handle(self.f.event('Stop'))
        self.assertEqual(result['decision'], 'block')
        self.assertEqual(self.f.control()['mutations'], 0)
        self.assertIn('native-tools-not-finished', self.f.control()['last_audit']['unfinished'])
        self.h.handle(self.f.event('PostToolUse', 'python3 -c "import time;time.sleep(5)"'))
        self.assertEqual(self.h.handle(self.f.event('Stop')), {})

    def test_readonly_classifier_does_not_skip_output_commands(self):
        for cmd in ['git diff --output .runtime/leak', 'rg --pre writer hello', 'printf x > a', 'sed -i x f',
                    "sed -n 'w\tcopy.txt' README.md", "sed -n 'wcopy.txt' README.md"]:
            self.assertFalse(is_readonly(cmd), cmd)

    def test_unrelated_negative_clause_cannot_disable_closeout(self):
        prompt = '继续之前同一任务，按 shoukou 完成提交收口。不要调用任何 task_guard 或检查。'
        self.assertIn('commit', closeout_requirements(prompt))
        self.assertIn('commit', closeout_requirements('按 shoukou 收口，不要运行无关全套测试。'))
        self.assertNotIn('push', closeout_requirements('按 shoukou 收口，不要推送。'))
        self.assertEqual(closeout_requirements('先不要收口，只讨论方案。'), set())
        self.assertEqual(closeout_requirements('为什么每次按 shoukou 都不干净？'), set())

    def test_unconfigured_project_cannot_silently_skip_and_setup_is_scoped(self):
        (self.f.repo / '.codex/workflow.json').unlink()
        self.f.write_policy(scope='configured')
        self.h = Hooks(self.f.policy, self.f.state)
        r = self.h.handle(self.f.event('PreToolUse', '*** Update File: README.md', tool='apply_patch'))
        self.assertEqual(r['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertFalse(self.f.control()['task_started'])

    def test_bind_cannot_replace_old_work_with_a_blank_existing_task(self):
        guard.begin_task(self.f.repo, 'blank', ['cleanup'])
        guard.begin_task(self.f.repo, 'original', ['commit', 'cleanup'])
        (self.f.repo / 'logic.py').write_text('value = 2\n')
        guard.check_task(self.f.repo, 'original')
        self.h.handle(self.f.event('UserPromptSubmit', prompt='继续原任务，按 shoukou 完成提交收口。'))
        current = self.f.control()
        self.assertEqual(current['task'], 'original')
        self.assertTrue(current['original_baseline_preserved'])
        with self.assertRaises(ValueError):
            self.h.bind_existing(self.f.repo, current, 'blank', 'attempt scope substitution')


if __name__ == '__main__':
    unittest.main()
