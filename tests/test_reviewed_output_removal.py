"""A reviewed historical deletion remains bound to its exact original output."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/task_guard.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('reviewed_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReviewedRemovalTests(unittest.TestCase):
    def test_storage_accepts_only_the_current_attributed_version(self):
        import storage_audit
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / '.gitignore').write_text('.runtime/\n')
            git('add', '.gitignore')
            git('commit', '-m', 'fixture')
            guard.init_project(repo, output_roots=['.runtime'])
            other = repo / 'other-design.html'
            other.write_text('old synthetic design')
            guard.begin_task(repo, 'selected', requirements=['cleanup', 'commit', 'local-main'])
            inventory = root / 'before.json.gz'
            storage_audit.save(inventory, storage_audit.capture(repo))
            keep = {'action': 'retained', 'retention': 'required',
                    'source': 'Synthetic fixture scope', 'reason': 'Fixture preservation'}
            full_review = {'before_inventory': str(inventory),
                           'before_sha256': storage_audit.digest(inventory),
                           'classifications': {str(repo): {
                               '.git/': dict(keep, kind='git-metadata'),
                               '.gitignore': dict(keep, kind='source'),
                               '.codex/': dict(keep, kind='source'),
                               'other-design.html': dict(keep, kind='business-data')}}}
            other.write_text('new synthetic design requested by its owner')
            review = {'lifecycle_task': 'selected'}
            self.assertEqual(storage_audit.lifecycle_owner_updates(repo, review), {})
            self.assertEqual(storage_audit.verify(repo, full_review)['status'], 'partial')
            finding = guard.check_task(repo, 'selected')['unfinished'][0]
            evidence = root / 'owner-evidence.json'
            evidence.write_text('{"fixture":"separate user asked original task to edit this design"}')
            decision = {'object': finding['object'], 'version': finding['version'],
                        'kind': 'owner-update', 'reason': 'Synthetic original owner request',
                        'evidence': {'path': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}}
            path = root / 'decision.json'
            path.write_text(json.dumps(decision))
            self.assertEqual(guard.check_task(repo, 'selected', decision_paths=[path])['state'], 'complete')
            self.assertEqual(set(storage_audit.lifecycle_owner_updates(repo, review)), {str(other)})
            self.assertEqual(storage_audit.verify(repo, dict(full_review, **review))['status'], 'complete')
            other.write_text('another unreviewed version')
            self.assertEqual(storage_audit.lifecycle_owner_updates(repo, review), {})
            self.assertEqual(storage_audit.verify(repo, dict(full_review, **review))['status'], 'partial')

    def test_main_changes_need_a_complete_original_lifecycle(self):
        import verify_closeout
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True,
                               capture_output=True)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / '.gitignore').write_text('.runtime/\n')
            git('add', '.gitignore')
            git('commit', '-m', 'fixture')
            guard.init_project(repo, output_roots=['.runtime'])
            other = repo / 'other-task.txt'
            other.write_text('preserved other work\n')
            guard.begin_task(repo, 'selected', requirements=['cleanup', 'commit', 'local-main'])
            self.assertFalse(verify_closeout.protected_main_accounted(repo, repo, {}))
            ledger = {'lifecycle_task': 'selected'}
            self.assertTrue(verify_closeout.protected_main_accounted(repo, repo, ledger))
            self.assertFalse(verify_closeout.protected_main_accounted(repo, repo / 'wrong', ledger))
            other.write_text('changed without accounting\n')
            self.assertFalse(verify_closeout.protected_main_accounted(repo, repo, ledger))
            other.write_text('preserved other work\n')
            (repo / 'unaccounted-new.txt').write_text('unexpected new work\n')
            self.assertFalse(verify_closeout.protected_main_accounted(repo, repo, ledger))

    def test_exact_receipt_required_and_unknown_removals_stay_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / 'repo'
            repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True,
                               capture_output=True)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / '.gitignore').write_text('.runtime/\n')
            git('add', '.gitignore')
            git('commit', '-m', 'fixture')
            guard.init_project(repo, output_roots=['.runtime'])
            output = repo / '.runtime'
            output.mkdir()
            for name in ['reviewed-copy.zip', 'other-copy.zip']:
                (output / name).write_bytes(b'synthetic historical copy')
            guard.begin_task(repo, 'cleanup', requirements=['cleanup'])
            for item in output.iterdir():
                item.unlink()
            result = guard.check_task(repo, 'cleanup')
            removed = {x['object']: x for x in result['unfinished']}
            self.assertEqual(len(removed), 2)
            selected = next(x for k, x in removed.items() if k.endswith('reviewed-copy.zip'))
            receipt = root / 'receipt.json'
            receipt.write_text('{"fixture_review":"original source and duplicate verified"}\n')
            decision = dict(object=selected['object'], version=selected['version'],
                            kind='reviewed-removal', reason='Fixture exact copy review',
                            evidence={'path': str(receipt), 'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest()})
            path = root / 'decision.json'
            path.write_text(json.dumps(dict(decision, version='stale-version')))
            self.assertEqual(len(guard.check_task(repo, 'cleanup', decision_paths=[path])['unfinished']), 2)
            path.write_text(json.dumps(decision))
            result = guard.check_task(repo, 'cleanup', decision_paths=[path])
            self.assertEqual(len(result['unfinished']), 1)
            self.assertTrue(result['unfinished'][0]['object'].endswith('other-copy.zip'))
            receipt.write_text('changed receipt\n')
            self.assertEqual(len(guard.check_task(repo, 'cleanup')['unfinished']), 2)


if __name__ == '__main__':
    unittest.main()
