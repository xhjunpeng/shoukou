"""Version isolation, dependency admission and synchronous management counterexamples."""
import copy
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_task_hooks import HookFixture, SCRIPTS, EVENTS
from hook_native_probe import native_run, native_hook_list, command, message, ENGINE
import install_task_hooks as installer
import manage_task_hooks as manager


class FrozenRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture()
        self.addCleanup(self.close)
        self.editable = self.f.root / 'editable'
        self.editable.mkdir()
        for name in installer.RUNTIME_FILES:
            shutil.copyfile(SCRIPTS / name, self.editable / name)
        self.report = {'candidates': {str(self.editable / n): installer.digest(self.editable / n)
                                      for n in installer.RUNTIME_FILES}}
        with patch.object(installer, 'HERE', self.editable):
            self.runtime = installer.freeze_runtime(self.f.home, self.report)
        pins = [{'path': str(self.runtime / n), 'sha256': installer.digest(self.runtime / n)}
                for n in installer.RUNTIME_FILES]
        self.f.write_policy(pinned_files=pins, runtime_release=str(self.runtime),
                            runtime_manifest_sha256=installer.digest(self.runtime / 'RUNTIME.json'),
                            management_entry={'path': str(self.editable / 'manage_task_hooks.py'),
                                              'sha256': installer.digest(self.runtime / 'manage_task_hooks.py')})
        handler = shlex.join([sys.executable, '-I', '-B', str(self.runtime / 'task_hooks.py'),
                              '--policy', str(self.f.policy), '--state-root', str(self.f.state)])
        self.config = {'hooks': {e: [{'hooks': [{'type': 'command', 'command': handler,
                              'timeout': 3 if e == 'Interrupt' else 20}]}] for e in EVENTS}}
        (self.f.home / 'hooks.json').write_text(json.dumps(self.config))

    def close(self):
        if hasattr(self, 'runtime') and self.runtime.exists():
            self.runtime.chmod(0o700)
        self.f.close()

    def states(self):
        return [json.loads(p.read_text()) for p in self.f.state.glob('*.json')]

    def evidence(self, result, control):
        return {'engine': result['engine'], 'exit_code': result['exit'],
                'request_count': result['request_count'], 'stdout': result['stdout'],
                'stderr': result['stderr'], 'control': control}

    @unittest.skipUnless(ENGINE.is_file(), 'Desktop engine unavailable')
    def test_installed_definitions_load_without_context_warnings(self):
        home = self.f.root / 'installed-home'; home.mkdir()
        report_path = self.f.root / 'accepted-report.json'
        self.report['engine_version'] = subprocess.check_output([str(ENGINE), '--version'], text=True).strip()
        report_path.write_text(json.dumps(self.report))
        with patch.object(installer, 'HERE', self.editable), patch.object(installer, 'validation', return_value=self.report):
            receipt = installer._install(home, self.f.repo, report_path, installer.digest(report_path))
        self.addCleanup(Path(receipt['runtime_release']).chmod, 0o700)
        config_path = home / 'hooks.json'
        accepted = config_path.read_bytes()
        good = json.loads(accepted)
        bad = copy.deepcopy(good)
        for event in ['Stop', 'Interrupt']:
            bad['hooks'][event][0]['hooks'][0]['additionalContextLimit'] = 800
        before = self.f.protected()
        config_path.write_text(json.dumps(bad))
        rejected_options = native_hook_list(home, self.f.repo)
        self.core_evidence = {'unsupported_options': rejected_options}
        self.assertEqual(rejected_options['exit_code'], 0, rejected_options['stderr'])
        warnings = json.dumps(rejected_options['listing'])
        for event in ['Stop', 'Interrupt']:
            self.assertIn('ignoring additionalContextLimit for ' + event + ' hook', warnings)
        config_path.write_bytes(accepted)
        loaded = native_hook_list(home, self.f.repo)
        self.core_evidence['installed_options'] = loaded
        self.assertEqual(loaded['exit_code'], 0, loaded['stderr'])
        self.assertEqual(len(loaded['listing']['data']), 1)
        group = loaded['listing']['data'][0]
        self.assertEqual(group['warnings'], [])
        self.assertEqual(group['errors'], [])
        self.assertEqual(len(group['hooks']), 5)
        result = native_run(home, self.f.repo,
                            [command("rg -n 'task|other' other.txt | head -n 2"), message('只读查询结束。')],
                            prompt='只读查看，不修改文件。')
        controls = [json.loads(p.read_text()) for p in (home / 'task-hooks/sessions').glob('*.json')]
        control = controls[-1]
        self.native_evidence = [self.evidence(result, control)]
        self.assertEqual(result['exit'], 0, result['stderr'])
        self.assertNotIn('ignoring additionalContextLimit', result['stderr'])
        self.assertTrue(result['request_count'])
        for event in ['Stop', 'Interrupt']:
            self.assertNotIn('additionalContextLimit', good['hooks'][event][0]['hooks'][0])
        for event in ['UserPromptSubmit', 'PreToolUse', 'PostToolUse']:
            self.assertEqual(good['hooks'][event][0]['hooks'][0]['additionalContextLimit'], 800)
        self.assertTrue(all(x['baseline_captures'] == 0 and not x['task_started'] for x in controls))
        self.assertEqual(before, self.f.protected())
        manager.verify_version(receipt)

    def test_freeze_requires_exact_accepted_bytes_and_never_overwrites_existing_release(self):
        (self.editable / 'task_guard.py').write_text('unaccepted source')
        with patch.object(installer, 'HERE', self.editable):
            with self.assertRaises(ValueError):installer.freeze_runtime(self.f.home, self.report)
        shutil.copyfile(SCRIPTS / 'task_guard.py', self.editable / 'task_guard.py')
        target = self.runtime / 'task_guard.py'; target.chmod(0o600);target.write_text('preserve drift')
        with patch.object(installer, 'HERE', self.editable):
            with self.assertRaises(ValueError):installer.freeze_runtime(self.f.home, self.report)
        self.assertEqual(target.read_text(), 'preserve drift')

    def test_source_alias_change_does_not_invalidate_frozen_management_version(self):
        receipt = {'policy': str(self.f.policy), 'policy_sha256': installer.digest(self.f.policy),
                   'pinned_files': [{'path': str(self.runtime / n), 'sha256': installer.digest(self.runtime / n)}
                                    for n in installer.RUNTIME_FILES],
                   'validation': {'path': str(self.f.policy), 'sha256': installer.digest(self.f.policy)},
                   'runtime_manifest': {'path': str(self.runtime / 'RUNTIME.json'),
                                        'sha256': installer.digest(self.runtime / 'RUNTIME.json')}}
        (self.editable / 'task_guard.py').write_text('another task in progress')
        manager.verify_version(receipt)
        target = self.runtime / 'RUNTIME.json';target.chmod(0o600);target.write_text('{}')
        with self.assertRaises(ValueError):manager.verify_version(receipt)

    def test_restore_rejects_unknown_runtime_member_but_uninstall_remains_safe(self):
        base = self.f.home / 'task-hooks'
        manager.write(base / 'installation.json', {'scope': 'pilot', 'managed_groups': self.config['hooks'],
            'policy': str(self.f.policy), 'policy_sha256': installer.digest(self.f.policy),
            'pinned_files': [{'path': str(self.runtime / n), 'sha256': installer.digest(self.runtime / n)}
                             for n in installer.RUNTIME_FILES],
            'validation': {'path': str(self.f.policy), 'sha256': installer.digest(self.f.policy)},
            'runtime_manifest': {'path': str(self.runtime / 'RUNTIME.json'),
                                 'sha256': installer.digest(self.runtime / 'RUNTIME.json')}})
        manager.manage(self.f.home, 'disable', True)
        before = (self.f.home / 'hooks.json').read_bytes()
        self.runtime.chmod(0o700)
        extra = self.runtime / 'extra.txt';extra.write_text('preserve unknown content')
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'enable', True)
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(), before)
        self.assertEqual(extra.read_text(), 'preserve unknown content')
        manager.manage(self.f.home, 'uninstall', True)
        self.assertEqual(extra.read_text(), 'preserve unknown content')
        self.assertEqual(manager.manage(self.f.home)['active_entries'], 0)

    @unittest.skipUnless(ENGINE.is_file(), 'Desktop engine unavailable')
    def test_development_edits_do_not_change_frozen_checks(self):
        before = self.f.protected()
        original = (self.runtime / 'task_guard.py').read_bytes()
        (self.editable / 'task_guard.py').write_text('raise RuntimeError("other task development must not execute")')
        query = native_run(self.f.home, self.f.repo, [command("rg -n 'task|other' other.txt | head -n 2"),
                           message('只读查询结束。')], prompt='只读查询，不改任何文件。')
        self.assertEqual(query['exit'], 0, query['stderr'])
        self.assertFalse(self.states()[0]['task_started'])
        self.assertEqual(self.states()[0]['baseline_captures'], 0)
        result = native_run(self.f.home, self.f.repo,
            [command('mkdir -p .runtime/test-workspaces/frozen && printf probe > .runtime/test-workspaces/frozen/copy.md'),
             message('完成。'), command('rm .runtime/test-workspaces/frozen/copy.md && rmdir .runtime/test-workspaces/frozen'),
             message('本轮产物已回收。')], prompt='只在指定临时位置验证自动检查，不提交、不运行项目测试。')
        controls = self.states();control = next(x for x in controls if x['task_started'])
        self.native_evidence = [self.evidence(result, control)]
        self.core_evidence = {'query': self.evidence(query, next(x for x in controls if not x['task_started']))}
        self.assertEqual(result['exit'], 0, result['stderr'])
        self.assertGreaterEqual(result['request_count'], 4)
        self.assertTrue(control['last_audit']['machine_checks_complete'])
        self.assertGreaterEqual(control['full_audits'], 2)
        self.assertEqual((self.runtime / 'task_guard.py').read_bytes(), original)
        self.assertEqual(before, self.f.protected())

    @unittest.skipUnless(ENGINE.is_file(), 'Desktop engine unavailable')
    def test_frozen_core_is_checked_before_import(self):
        sentinel = self.f.root / 'must-not-execute'
        core = self.runtime / 'task_guard.py';core.chmod(0o600)
        core.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').write_text("bad")\n')
        check = subprocess.run([sys.executable, '-I', '-B', str(self.runtime / 'task_hooks.py'),
                                '--policy', str(self.f.policy), '--state-root', str(self.f.state)],
                               input=json.dumps(self.f.event('PreToolUse', 'printf SHOULD_NOT_EXECUTE')),
                               capture_output=True, text=True, check=True)
        denied = json.loads(check.stdout)['hookSpecificOutput']
        self.assertEqual(denied['permissionDecision'], 'deny')
        self.assertIn('Protected hook/checker changed', denied['permissionDecisionReason'])
        result = native_run(self.f.home, self.f.repo, [command('printf SHOULD_NOT_EXECUTE'), message('结束。')],
                            prompt='只读版本完整性反例，不改业务文件。')
        self.native_evidence = [self.evidence(result, {'task_started': False})]
        self.core_evidence = {'bootstrap_denial': denied, 'stdout': check.stdout, 'stderr': check.stderr}
        self.assertEqual(result['exit'], 0, result['stderr'])
        self.assertEqual(result['request_count'], 0)  # UserPromptSubmit rejects before any model request.
        self.assertFalse(sentinel.exists())
        self.assertFalse(self.states())

    @unittest.skipUnless(ENGINE.is_file(), 'Desktop engine unavailable')
    def test_global_management_alias_is_synchronous_with_frozen_runtime(self):
        before = self.f.protected()
        base = self.f.home / 'task-hooks'
        foreign = {'hooks': [{'type': 'command', 'command': 'printf foreign'}]}
        cfg = copy.deepcopy(self.config);cfg['hooks']['Stop'].append(foreign)
        manager.write(self.f.home / 'hooks.json', cfg)
        manager.write(base / 'installation.json', {'scope': 'pilot', 'managed_groups': self.config['hooks'],
            'policy': str(self.f.policy), 'policy_sha256': installer.digest(self.f.policy),
            'pinned_files': [{'path': str(self.runtime / n), 'sha256': installer.digest(self.runtime / n)}
                             for n in installer.RUNTIME_FILES],
            'validation': {'path': str(self.f.policy), 'sha256': installer.digest(self.f.policy)},
            'runtime_manifest': {'path': str(self.runtime / 'RUNTIME.json'),
                                 'sha256': installer.digest(self.runtime / 'RUNTIME.json')}})
        cmd = shlex.join([sys.executable, '-B', str(self.editable / 'manage_task_hooks.py'), '--home', str(self.f.home)])
        result = native_run(self.f.home, self.f.repo, [command(cmd+' disable --apply'),
            command(cmd+' enable --apply'), command(cmd+' uninstall --apply'), message('仅管理入口已卸载。')],
            prompt='只验证安全停用、恢复和卸载；不修改项目成果或业务数据。')
        control = self.states()[0]
        self.native_evidence = [self.evidence(result, control)]
        self.assertEqual(result['exit'], 0, result['stderr'])
        self.assertEqual(control['pending_tools'], {})
        self.assertFalse(control['task_started'])
        self.assertEqual(manager.read(self.f.home / 'hooks.json')['hooks']['Stop'], [foreign])
        self.assertEqual(before, self.f.protected())
