"""Behavioral tests for read-only classification and reversible entry removal."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest.mock import patch

from test_task_hooks import HookFixture, SCRIPTS
from hook_native_probe import native_run, command, message, ENGINE
from readonly_query import readonly_command
import manage_task_hooks as manager

HANDLER = Path(os.environ.get('CODEX_TEST_HANDLER', str(SCRIPTS / 'task_hooks.py')))
spec = importlib.util.spec_from_file_location('tested_query_hook', HANDLER)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class ReadonlyQueries(unittest.TestCase):
    def test_quoted_metacharacters_and_read_pipeline_are_queries(self):
        for cmd in ["rg -n 'foo|bar' package.json", "rg -n 'x>;y' .codex/workflow.json",
                    "rg -n 'foo|bar' package.json | head -n 2", "printf '%s' 'a;b>c|d'",
                    "sed -n '1,2p' package.json"]:
            self.assertEqual(readonly_command(cmd), cmd, cmd)

    def test_hidden_writes_are_not_readonly(self):
        for cmd in ["rg x package.json > overwritten", 'rg "$(touch hidden)" package.json',
                    'rg "`touch hidden`" package.json', 'cat package.json | tee overwritten',
                    "sed -n 'w\tcopy.txt' package.json", 'rg --pre writer x package.json',
                    "git diff --output=overwritten", 'cat package.json; touch hidden',
                    'python3 -c "print(\'$(touch hidden)\')"',
                    "python3 -c 'from pathlib import Path;Path(\"other.txt\").write_text(\"bad\")'",
                    "python3 -c 'eval(\"1\")'",
                    "python3 -c 'sorted([\"print(1)\"],key=exec)'",
                    "python3 -c 'import json;json.loads(\"{}\",object_hook=eval)'", "python3 -c 'import subprocess;subprocess.run([\"true\"])'",
                    "python3 -c 'print(1,file=Path(\"x\"))'",
                    "python3 - <<PY\nprint('$(touch hidden)')\nPY"]:
            self.assertIsNone(readonly_command(cmd), cmd)

    def test_data_python_is_isolated_and_actual_protected_read_does_not_start_task(self):
        f = HookFixture()
        try:
            (f.repo / 'other.txt').write_text('existing other task\n')
            h = module.Hooks(f.policy, f.state)
            h.handle(f.event('UserPromptSubmit', prompt='只查询，不修改'))
            cmd = "python3 -B - <<'PY'\nimport json\nfrom pathlib import Path\nprint(json.loads(Path('.codex/workflow.json').read_text()))\nPY"
            r = h.handle(f.event('PreToolUse', cmd))
            rewritten = r['hookSpecificOutput']['updatedInput']['command']
            self.assertIn('-I', shlex.split(rewritten))
            self.assertFalse(f.control()['task_started'])
            self.assertEqual(h.handle(f.event('PreToolUse', "rg -n 'foo|bar' other.txt")), {})
        finally:
            f.close()


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture(); self.addCleanup(self.f.close)
        self.base = self.f.home / 'task-hooks'; self.base.mkdir()
        self.evidence = self.base / 'acceptance.json'; self.evidence.write_text('{"fixture":true}')
        self.cfg = json.loads((self.f.home / 'hooks.json').read_text())
        self.cfg['foreign_setting'] = {'keep': True}
        self.cfg['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'printf foreign'}]})
        self.managed = {e: [copy.deepcopy(groups[0])] for e, groups in self.cfg['hooks'].items()}
        (self.f.home / 'hooks.json').write_text(json.dumps(self.cfg))
        r = {'scope': 'pilot', 'hooks_sha256': manager.digest((self.f.home / 'hooks.json').read_bytes()),
             'policy': str(self.f.policy), 'policy_sha256': manager.digest(self.f.policy.read_bytes()),
             'pinned_files': [{'path': str(HANDLER), 'sha256': manager.digest(HANDLER.read_bytes())}],
             'validation': {'path': str(self.evidence), 'sha256': manager.digest(self.evidence.read_bytes())},
             'managed_groups': self.managed}
        manager.write(self.base / 'installation.json', r)
        self.source = (self.f.repo / 'other.txt').read_bytes()
        self.data = self.f.repo / '.runtime/business-data'; self.data.mkdir(parents=True)
        (self.data / 'keep').write_text('business')
        self.history = self.f.repo / '.git/history-from-other-task'; self.history.write_text('keep')

    def cfg_now(self):
        return json.loads((self.f.home / 'hooks.json').read_text())

    def assert_preserved(self):
        self.assertEqual((self.f.repo / 'other.txt').read_bytes(), self.source)
        self.assertEqual((self.data / 'keep').read_text(), 'business')
        self.assertEqual(self.history.read_text(), 'keep')
        self.assertTrue(self.evidence.exists())
        self.assertTrue(HANDLER.exists())

    def test_dry_run_disable_restore_merge_and_uninstall_are_scoped(self):
        original = (self.f.home / 'hooks.json').read_bytes()
        self.assertEqual(manager.manage(self.f.home, 'disable')['state'], 'planned')
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(), original)
        manager.manage(self.f.home, 'disable', True)
        self.assertEqual(self.cfg_now()['hooks']['Stop'], self.cfg['hooks']['Stop'][1:])
        cfg = self.cfg_now(); cfg['new_foreign'] = 42
        late = {'hooks': [{'type': 'command', 'command': 'printf late-other-task'}]}
        cfg['hooks']['Stop'].append(late); manager.write(self.f.home / 'hooks.json', cfg)
        manager.manage(self.f.home, 'enable', True)
        self.assertIn(late, self.cfg_now()['hooks']['Stop'])
        self.assertEqual(self.cfg_now()['new_foreign'], 42)
        manager.manage(self.f.home, 'uninstall', True)
        self.assertEqual(manager.manage(self.f.home, 'status')['active_entries'], 0)
        before = (self.f.home / 'hooks.json').read_bytes()
        manager.manage(self.f.home, 'uninstall', True)
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(), before)
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'enable', True)
        self.assert_preserved()

    def test_missing_receipt_changed_entry_and_symlink_are_refused(self):
        path = self.f.home / 'hooks.json'; original = path.read_bytes()
        cfg = self.cfg_now(); cfg['hooks']['Stop'][0]['hooks'][0]['timeout'] += 1
        manager.write(path, cfg)
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'disable', True)
        manager.write(path, self.cfg)
        saved = self.base / 'installation.json'; text = saved.read_bytes(); saved.unlink()
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'uninstall', True)
        saved.write_bytes(text)
        target = self.f.root / 'foreign-config'; target.write_bytes(original); path.unlink();path.symlink_to(target)
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'disable', True)
        self.assertEqual(target.read_bytes(), original)
        path.unlink(); path.write_bytes(original)
        self.assert_preserved()

    def test_source_drift_blocks_restore_but_never_blocks_disable(self):
        r = manager.read(self.base / 'installation.json');r['pinned_files'][0]['sha256'] = '0'*64
        manager.write(self.base / 'installation.json', r)
        manager.manage(self.f.home, 'disable', True)
        before = (self.f.home / 'hooks.json').read_bytes()
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'enable', True)
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(), before)
        self.assert_preserved()

    def test_restore_recovery_rechecks_source_and_can_be_safely_cancelled(self):
        manager.manage(self.f.home, 'disable', True)
        real_write = manager.write
        def interrupted(path, value):
            if path == self.f.home / 'hooks.json':raise OSError('simulated restore interruption')
            return real_write(path, value)
        with patch.object(manager, 'write', side_effect=interrupted):
            with self.assertRaises(OSError):manager.manage(self.f.home, 'enable', True)
        receipt = manager.read(self.base / 'installation.json')
        receipt['pinned_files'][0]['sha256'] = '0'*64
        real_write(self.base / 'installation.json',receipt)
        before = (self.f.home / 'hooks.json').read_bytes()
        with self.assertRaises(ValueError):manager.manage(self.f.home,'recover',True)
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(),before)
        manager.manage(self.f.home,'disable',True)
        self.assertNotIn('pending',manager.read(self.base / 'management.json'))
        self.assertEqual(manager.manage(self.f.home)['active_entries'],0)
        self.assert_preserved()

    def test_duplicate_entries_cannot_be_reported_as_disabled(self):
        cfg=self.cfg_now();cfg['hooks']['Stop'].append(copy.deepcopy(self.managed['Stop'][0]))
        manager.write(self.f.home / 'hooks.json',cfg)
        before=(self.f.home / 'hooks.json').read_bytes()
        with self.assertRaises(ValueError):manager.manage(self.f.home,'disable',True)
        self.assertEqual((self.f.home / 'hooks.json').read_bytes(),before)

    def test_interrupted_transaction_can_recover_without_overwriting_new_config(self):
        real_write = manager.write
        original_bytes = (self.f.home / 'hooks.json').read_bytes()
        def interrupted(path, value):
            if path == self.f.home / 'hooks.json':raise OSError('simulated interruption')
            return real_write(path, value)
        with patch.object(manager, 'write', side_effect=interrupted):
            with self.assertRaises(OSError):manager.manage(self.f.home, 'disable', True)
        self.assertIn('pending', manager.read(self.base / 'management.json'))
        cfg = self.cfg_now();cfg['foreign_after_crash'] = True;real_write(self.f.home / 'hooks.json',cfg)
        with self.assertRaises(ValueError):manager.manage(self.f.home, 'recover', True)
        (self.f.home / 'hooks.json').write_bytes(original_bytes)
        manager.manage(self.f.home, 'recover', True)
        self.assertEqual(manager.manage(self.f.home)['active_entries'],0)
        self.assert_preserved()


@unittest.skipUnless(ENGINE.is_file(), 'Native engine required')
class NativeQueries(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture();self.addCleanup(self.f.close)
        cmd = shlex.join([sys.executable,'-B',str(HANDLER),'--policy',str(self.f.policy),'--state-root',str(self.f.state)])
        self.cfg={'hooks':{e:[{'hooks':[{'type':'command','command':cmd,'timeout':20}]}] for e in module.EVENTS}}
        (self.f.home/'hooks.json').write_text(json.dumps(self.cfg))

    def save_evidence(self,r):
        self.assertEqual(r['exit'],0,r['stderr'])
        self.native_evidence=[{'engine':r['engine'],'exit_code':r['exit'],'request_count':r['request_count'],
                              'stdout':r['stdout'],'stderr':r['stderr'],'control':self.f.control()}]

    def test_native_readonly_queries_allow_dirty_paths_without_baseline(self):
        (self.f.repo/'other.txt').write_text('other task changed\n');self.f.git('add','other.txt')
        before=self.f.protected()
        (self.f.repo/'json.py').write_text("from pathlib import Path\nPath('hijacked').write_text('bad')\n")
        query="python3 -B - <<'PY'\nimport json\nfrom pathlib import Path\nprint(json.loads(Path('.codex/workflow.json').read_text()))\nPY"
        r=native_run(self.f.home,self.f.repo,[command("rg -n 'task|changed' other.txt | head -n 2"),command(query),message('查询完成，未改文件。')],prompt='只读查询，保留所有已有成果。')
        self.save_evidence(r)
        self.assertFalse(self.f.control()['task_started'])
        self.assertFalse((self.f.repo/'hijacked').exists())
        self.assertEqual(self.f.control()['baseline_captures'],0)
        self.assertEqual(before['index'],self.f.protected()['index'])
        self.assertEqual(before['other'],self.f.protected()['other'])
        self.assertTrue(any('schema_version' in str(q.get('input')) for q in r['requests']))

    def test_native_disable_restore_and_uninstall_preserve_other_hook(self):
        base=self.f.home/'task-hooks';base.mkdir()
        evidence=base/'acceptance.json';evidence.write_text('{}')
        foreign={'hooks':[{'type':'command','command':'printf foreign'}]}
        cfg=copy.deepcopy(self.cfg);cfg['hooks']['Stop'].append(foreign)
        manager.write(self.f.home/'hooks.json',cfg)
        manager.write(base/'installation.json',{'scope':'pilot','managed_groups':self.cfg['hooks'],
            'policy':str(self.f.policy),'policy_sha256':manager.digest(self.f.policy.read_bytes()),
            'pinned_files':[{'path':str(HANDLER),'sha256':manager.digest(HANDLER.read_bytes())}],
            'validation':{'path':str(evidence),'sha256':manager.digest(evidence.read_bytes())}})
        manager.manage(self.f.home,'disable',True)
        r=native_run(self.f.home,self.f.repo,[command("printf disabled"),message('已停用')],prompt='仅执行临时正控')
        self.assertEqual(r['exit'],0);self.assertFalse(self.f.state.exists())
        manager.manage(self.f.home,'enable',True)
        control_cmd=shlex.join([sys.executable,'-B',str(SCRIPTS/'manage_task_hooks.py'),'--home',str(self.f.home)])
        r=native_run(self.f.home,self.f.repo,[command(control_cmd+' disable --apply'),command(control_cmd+' enable --apply'),command("mkdir -p .runtime/test-workspaces/probe && printf test > .runtime/test-workspaces/probe/copy.md"),message('完成'),command("rm .runtime/test-workspaces/probe/copy.md && rmdir .runtime/test-workspaces/probe"),message('已回收')],prompt='在隔离临时目录验证自动检查，不提交。')
        self.save_evidence(r)
        self.assertGreaterEqual(r['request_count'],4)
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        manager.manage(self.f.home,'uninstall',True)
        self.assertEqual(manager.read(self.f.home/'hooks.json')['hooks']['Stop'],[foreign])
        self.assertEqual(self.f.task()['runs'],[])
        self.assertEqual(self.f.control()['pending_tools'],{})
