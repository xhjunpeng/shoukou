"""The normal closeout path must not invoke full-directory cleanup audits."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'inspect_repo.py'
spec = importlib.util.spec_from_file_location('daily_inspect', SCRIPT)
inspect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspect)


class DailyInspectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shoukou-daily-')
        self.root = Path(self.temp.name).resolve()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / '.gitignore').write_text('.runtime/\nnode_modules/\n')
        (self.root / 'page.txt').write_text('before\n')
        self.git('add', '.gitignore', 'page.txt')
        self.git('commit', '-m', 'fixture')
        (self.root / '.runtime').mkdir()
        (self.root / '.runtime' / 'unknown-database').write_bytes(b'must remain')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def test_ignored_files_do_not_block_code_observation_and_remain(self):
        out = inspect.daily_inventory(self.root, 'main')
        self.assertTrue(out['tracked_and_untracked_clean'])
        self.assertEqual(out['ignored_files'], 'not-scanned')
        self.assertFalse(out['cleanup_authorized'])
        self.assertEqual((self.root / '.runtime/unknown-database').read_bytes(), b'must remain')

    def test_does_not_walk_files_or_query_other_worktrees_network_or_ledger(self):
        with patch.object(inspect, 'snapshot', side_effect=AssertionError('full snapshot')), \
             patch.object(inspect, 'inventory', side_effect=AssertionError('full inventory')), \
             patch.object(inspect, 'worktrees', side_effect=AssertionError('other trees')), \
             patch.object(inspect, 'github', side_effect=AssertionError('network')), \
             patch.object(inspect.os, 'walk', side_effect=AssertionError('filesystem walk')):
            out = inspect.daily_inventory(self.root, 'main')
        self.assertEqual(out['other_worktrees'], 'not-scanned')
        self.assertEqual(out['remote'], 'not-queried')
        self.assertFalse((self.root / '.codex').exists())

    def test_dirty_and_untracked_work_is_reported_not_removed(self):
        (self.root / 'page.txt').write_text('changed\n')
        (self.root / 'new.txt').write_text('unique\n')
        out = inspect.daily_inventory(self.root, 'main')
        self.assertFalse(out['tracked_and_untracked_clean'])
        self.assertIn('page.txt', out['unstaged'])
        self.assertIn('new.txt', out['status'])
        self.assertEqual((self.root/'new.txt').read_text(), 'unique\n')

    def test_staged_work_and_index_are_unchanged(self):
        (self.root/'page.txt').write_text('staged\n')
        self.git('add', 'page.txt')
        before = self.git('diff', '--cached', '--binary')
        out = inspect.daily_inventory(self.root, 'main')
        self.assertEqual(out['staged'], ['page.txt'])
        self.assertEqual(before, self.git('diff', '--cached', '--binary'))

    def test_cli_daily_output_never_authorizes_cleanup(self):
        run = subprocess.run(['python3','-B',str(SCRIPT),'--daily','--repo',str(self.root),'--base','main','--json'],capture_output=True,text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual(out['mode'],'daily-scoped-v2')
        self.assertFalse(out['cleanup_authorized'])
        self.assertNotIn('cleanup_preflight', out)

    def test_daily_cannot_be_used_as_full_cleanup_evidence(self):
        for flags in [['--check-cleanup',str(self.root),'--evidence','missing.json'],['--artifacts'],['--worktree',str(self.root)],['--summary']]:
            run=subprocess.run(['python3','-B',str(SCRIPT),'--daily','--repo',str(self.root),*flags],capture_output=True,text=True)
            self.assertNotEqual(run.returncode,0)
            self.assertEqual((self.root/'.runtime/unknown-database').read_bytes(),b'must remain')

    def test_subdirectory_cannot_silently_change_scope(self):
        sub=self.root/'sub';sub.mkdir()
        with self.assertRaises(ValueError):inspect.daily_inventory(sub,'main')

    def test_default_skill_has_no_mandatory_full_scan_or_empty_ledger(self):
        skill=(SCRIPT.parents[1]/'SKILL.md').read_text()
        self.assertIn('--daily',skill)
        self.assertIn('没有独立资源就不建空台账',skill)
        self.assertIn('日常路径不运行全目录',skill)
        self.assertIn('extended-closeout.md',skill)
        self.assertTrue((SCRIPT.parents[1]/'references/cleanup-check.md').is_file())


if __name__ == '__main__':
    unittest.main()
