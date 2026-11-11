#!/usr/bin/env python3
"""Branch-only checks preserve dirty source in isolated worktrees."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import branch_cleanup as checker


class BranchCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shoukou-branch-', dir=Path.cwd())
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'main'
        self.wt = self.root / 'old task'
        self.g('init', '-b', 'main', str(self.repo), cwd=self.root)
        self.g('config', 'user.name', 'Test')
        self.g('config', 'user.email', 'test@example.invalid')
        (self.repo / 'source.txt').write_text('baseline\n')
        self.g('add', '.')
        self.g('commit', '-m', 'baseline')
        self.head = self.g('rev-parse', 'HEAD')
        self.g('worktree', 'add', '-b', 'dev/old', str(self.wt))
        (self.repo / 'source.txt').write_text('delivered version\n')
        (self.repo / 'new.txt').write_text('delivered new file\n')
        self.g('add', '.')
        self.g('commit', '-m', 'delivered')
        self.delivered = self.g('rev-parse', 'HEAD')
        # Keep an old version in the worktree, not the latest main file.
        (self.repo / 'source.txt').write_text('later main version\n')
        self.g('commit', '-am', 'later')
        (self.wt / 'source.txt').write_text('delivered version\n')
        (self.wt / 'new.txt').write_text('delivered new file\n')

    def tearDown(self):
        self.tmp.cleanup()

    def g(self, *args, cwd=None):
        r = subprocess.run(['git', '-C', str(cwd or self.repo), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def evidence(self):
        state = checker.observe(self.repo, self.wt, 'main')
        return {'schema_version': 1, 'observed_at_unix': time.time(), 'mode': 'full',
                'cleanup_authorized': True, 'target': state['target'],
                'task': {'ownership_confirmed': True, 'completed': True, 'accepted': True,
                         'source': 'fixture owner and acceptance record'},
                'dependencies': {'checked': True, 'active_writers': [], 'requires_branch': False,
                                 'source': 'fixture task and process query'},
                'delivered_file_commits': {'source.txt': self.delivered, 'new.txt': self.delivered}}

    def check(self, evidence=None, preserved=False):
        return checker.check(self.repo, self.wt, 'dev/old', 'main', evidence or self.evidence(), preserved)

    def test_delivered_dirty_files_pass_without_mutation(self):
        before = self.g('status', '--porcelain=v1', cwd=self.wt)
        refs = self.g('show-ref')
        self.assertEqual(self.check()['branch_cleanup'], 'passed')
        self.assertEqual(before, self.g('status', '--porcelain=v1', cwd=self.wt))
        self.assertEqual(refs, self.g('show-ref'))

    def test_same_head_detach_and_branch_removal_preserve_dirty_files(self):
        evidence = self.evidence()
        self.assertEqual(self.check(evidence)['branch_cleanup'], 'passed')
        original = (self.wt / 'source.txt').read_bytes()
        self.g('switch', '--detach', self.head, cwd=self.wt)
        self.g('branch', '-d', 'dev/old')
        self.assertEqual(self.check(evidence, preserved=True)['branch_cleanup'], 'preserved')
        self.assertEqual((self.wt / 'source.txt').read_bytes(), original)
        self.assertIn('source.txt', self.g('status', '--porcelain=v1', cwd=self.wt))
        self.assertTrue((self.wt / 'new.txt').is_file())

    def test_unique_work_cannot_use_history_copy_exception(self):
        (self.wt / 'source.txt').write_text('not delivered\n')
        result = self.check()
        self.assertEqual(result['branch_cleanup'], 'blocked')
        self.assertIn('file-not-in-delivered-version:source.txt', result['blockers'])

    def test_identical_blob_at_another_path_is_not_delivery_proof(self):
        (self.wt / 'other.txt').write_text('delivered new file\n')
        evidence = self.evidence()
        evidence['delivered_file_commits']['other.txt'] = self.delivered
        self.assertIn('file-not-in-delivered-version:other.txt', self.check(evidence)['blockers'])

    def test_unmerged_source_commit_is_rejected(self):
        self.g('branch', 'dev/unmerged')
        self.g('worktree', 'add', str(self.root / 'unmerged'), 'dev/unmerged')
        p = self.root / 'unmerged'
        (p / 'source.txt').write_text('delivered version\n')
        self.g('commit', '-am', 'not merged', cwd=p)
        evidence = self.evidence()
        evidence['delivered_file_commits']['source.txt'] = self.g('rev-parse', 'HEAD', cwd=p)
        self.assertIn('file-version-not-in-base:source.txt', self.check(evidence)['blockers'])

    def test_staged_work_cannot_be_hidden_by_matching_worktree_file(self):
        (self.wt / 'source.txt').write_text('unique staged work\n')
        self.g('add', 'source.txt', cwd=self.wt)
        (self.wt / 'source.txt').write_text('delivered version\n')
        self.assertIn('staged-work-requires-normal-delivery', self.check()['blockers'])

    def test_unconfirmed_owner_active_writer_and_stage_mode_block(self):
        for field, value, expected in [('task', {'ownership_confirmed': False}, 'task-ownership-or-completion-unconfirmed'),
                                       ('task', {'completed': False}, 'task-ownership-or-completion-unconfirmed'),
                                       ('task', {'accepted': False}, 'task-ownership-or-completion-unconfirmed'),
                                       ('dependencies', {'active_writers': [123]}, 'branch-dependencies-or-writers-unresolved'),
                                       ('dependencies', {'requires_branch': True}, 'branch-dependencies-or-writers-unresolved')]:
            evidence = self.evidence()
            evidence[field].update(value)
            with self.subTest(field=field, value=value):
                self.assertIn(expected, self.check(evidence)['blockers'])
        evidence = self.evidence()
        evidence['mode'] = 'stage'
        self.assertIn('full-closeout-not-authorized', self.check(evidence)['blockers'])

    def test_changed_file_and_base_invalidate_observation(self):
        evidence = self.evidence()
        (self.wt / 'new.txt').write_text('changed after evidence\n')
        self.assertIn('target-changed-since-evidence', self.check(evidence)['blockers'])
        evidence = self.evidence()
        self.g('commit', '--allow-empty', '-m', 'new base')
        self.assertIn('target-changed-since-evidence', self.check(evidence)['blockers'])

    def test_source_change_during_check_cannot_pass(self):
        evidence = self.evidence()
        real_observe = checker.observe
        calls = []
        def concurrent_write(*args):
            state = real_observe(*args)
            calls.append(True)
            if len(calls) == 1:
                (self.wt / 'new.txt').write_text('concurrent change\n')
            return state
        with patch.object(checker, 'observe', side_effect=concurrent_write):
            self.assertIn('target-changed-during-check', self.check(evidence)['blockers'])

    def test_unknown_extra_file_and_stale_evidence_block(self):
        (self.wt / 'unknown.txt').write_text('unknown')
        evidence = self.evidence()
        self.assertIn('delivered-file-list-not-exact', self.check(evidence)['blockers'])
        evidence['observed_at_unix'] -= 121
        self.assertIn('observation-not-fresh', self.check(evidence)['blockers'])

    def test_symbolic_link_and_hidden_index_flags_are_rejected(self):
        (self.wt / 'new.txt').unlink()
        (self.wt / 'new.txt').symlink_to(self.repo / 'new.txt')
        self.assertIn('unsupported-source-type:new.txt', self.check()['blockers'])
        (self.wt / 'new.txt').unlink()
        (self.wt / 'new.txt').write_text('delivered new file\n')
        self.g('update-index', '--assume-unchanged', 'source.txt', cwd=self.wt)
        self.assertIn('hidden-index-state', self.check()['blockers'])

    def test_post_check_detects_changed_files_and_remaining_branch(self):
        evidence = self.evidence()
        self.assertIn('branch-or-checkout-still-present', self.check(evidence, preserved=True)['blockers'])
        self.g('switch', '--detach', self.head, cwd=self.wt)
        self.g('branch', '-d', 'dev/old')
        (self.wt / 'new.txt').write_text('lost original')
        self.assertIn('source-not-preserved', self.check(evidence, preserved=True)['blockers'])

    def test_cli_observation_preflight_and_postcheck(self):
        args = [sys.executable, checker.__file__, '--repo', str(self.repo),
                '--worktree', str(self.wt), '--base', 'main']
        observed = subprocess.run([*args, '--observe'], capture_output=True, text=True)
        self.assertEqual(observed.returncode, 0, observed.stdout)
        self.assertEqual(json.loads(observed.stdout)['target'], self.evidence()['target'])
        evidence = self.root / 'branch-evidence.json'
        evidence.write_text(json.dumps(self.evidence()))
        checked = subprocess.run([*args, '--branch', 'dev/old', '--evidence', str(evidence)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertEqual(json.loads(checked.stdout)['branch_cleanup'], 'passed')
        self.g('switch', '--detach', self.head, cwd=self.wt)
        self.g('branch', '-d', 'dev/old')
        checked = subprocess.run([*args, '--branch', 'dev/old', '--evidence', str(evidence), '--verify-preserved'], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertEqual(json.loads(checked.stdout)['branch_cleanup'], 'preserved')

    def test_cli_refuses_evidence_stored_inside_target(self):
        evidence = self.wt / 'branch-evidence.json'
        evidence.write_text(json.dumps(self.evidence()))
        result = subprocess.run([sys.executable, checker.__file__, '--repo', str(self.repo),
                                 '--worktree', str(self.wt), '--branch', 'dev/old', '--base', 'main',
                                 '--evidence', str(evidence)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('outside the worktree', json.loads(result.stdout)['error'])

    def test_coordinator_cli_closes_multiple_worktrees_and_preserves_all_files(self):
        second = self.root / 'another old task'
        self.g('worktree', 'add', '-b', 'dev/other', str(second), self.head)
        # Ignored runtime data is outside the branch-only check; it must remain.
        (self.repo / '.git/info/exclude').write_text('.runtime/\n')
        targets = [(self.wt, 'dev/old', self.evidence()), (second, 'dev/other', None)]
        checks = []
        for target, branch, evidence in targets:
            runtime = target / '.runtime'
            runtime.mkdir()
            (runtime / 'retained.db').write_bytes(b'unique retained fixture data')
            args = [sys.executable, checker.__file__, '--repo', str(self.repo),
                    '--worktree', str(target), '--base', 'main']
            observed = subprocess.run([*args, '--observe'], cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(observed.returncode, 0, observed.stdout)
            if evidence is None:
                evidence = self.evidence()
                evidence['target'] = json.loads(observed.stdout)['target']
                evidence['delivered_file_commits'] = {}
            record = self.root / (branch.split('/')[-1] + '-evidence.json')
            record.write_text(json.dumps(evidence))
            args.extend(['--branch', branch, '--evidence', str(record)])
            checked = subprocess.run(args, cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout)
            self.assertEqual(json.loads(checked.stdout)['branch_cleanup'], 'passed')
            original = {str(p.relative_to(target)): p.read_bytes()
                        for p in target.rglob('*') if p.is_file()}
            index = self.g('ls-files', '--stage', cwd=target)
            status = self.g('status', '--porcelain=v1', cwd=target)
            checks.append((target, branch, args, original, index, status))
        # All mutations are issued from the coordinating checkout, using exact -C paths.
        for target, branch, args, original, index, status in checks:
            for command in (['git', '-C', str(target), 'switch', '--detach', self.head],
                            ['git', '-C', str(self.repo), 'branch', '-d', branch]):
                result = subprocess.run(command, cwd=self.repo, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            checked = subprocess.run([*args, '--verify-preserved'], cwd=self.repo,
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout)
            self.assertEqual(json.loads(checked.stdout)['branch_cleanup'], 'preserved')
            self.assertEqual(original, {str(p.relative_to(target)): p.read_bytes()
                                        for p in target.rglob('*') if p.is_file()})
            self.assertEqual(index, self.g('ls-files', '--stage', cwd=target))
            self.assertEqual(status, self.g('status', '--porcelain=v1', cwd=target))
            self.assertEqual(self.g('rev-parse', 'HEAD', cwd=target), self.head)
            self.assertTrue(target.is_dir())
        self.assertEqual(self.g('branch', '--show-current'), 'main')
        self.assertEqual(self.g('status', '--porcelain=v1'), '')

    def test_coordinator_cli_rejects_a_different_repository(self):
        other = self.root / 'unrelated project'
        self.g('init', '-b', 'main', str(other), cwd=self.root)
        self.g('config', 'user.name', 'Other fixture', cwd=other)
        self.g('config', 'user.email', 'other@example.invalid', cwd=other)
        (other / 'source.txt').write_text('unrelated source')
        self.g('add', '.', cwd=other)
        self.g('commit', '-m', 'unrelated fixture', cwd=other)
        refs = self.g('show-ref', cwd=other)
        result = subprocess.run([sys.executable, checker.__file__, '--repo', str(self.repo),
                                 '--worktree', str(other), '--base', 'main', '--observe'],
                                cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('selected repository', json.loads(result.stdout)['error'])
        self.assertEqual(refs, self.g('show-ref', cwd=other))
        self.assertEqual((other / 'source.txt').read_text(), 'unrelated source')

    def test_coordinator_cli_rejects_an_unmerged_branch_head_without_mutation(self):
        self.g('commit', '--allow-empty', '-m', 'unmerged task head', cwd=self.wt)
        evidence = self.root / 'unmerged-evidence.json'
        evidence.write_text(json.dumps(self.evidence()))
        refs = self.g('show-ref')
        status = self.g('status', '--porcelain=v1', cwd=self.wt)
        result = subprocess.run([sys.executable, checker.__file__, '--repo', str(self.repo),
                                 '--worktree', str(self.wt), '--base', 'main', '--branch', 'dev/old',
                                 '--evidence', str(evidence)], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('original-branch-head-not-in-base', json.loads(result.stdout)['blockers'])
        self.assertEqual(refs, self.g('show-ref'))
        self.assertEqual(status, self.g('status', '--porcelain=v1', cwd=self.wt))
        self.assertEqual(self.g('branch', '--show-current', cwd=self.wt), 'dev/old')


if __name__ == '__main__':
    unittest.main()
