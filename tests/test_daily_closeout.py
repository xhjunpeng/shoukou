"""Behavioral checks for the bounded, read-only daily final verifier."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_daily_closeout.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('daily_closeout', SCRIPT)
verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_module)


class DailyCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shoukou-daily-final-')
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'original'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'source.txt').write_text('saved implementation\n')
        (self.repo / 'other.txt').write_text('other original\n')
        self.git('add', 'source.txt', 'other.txt')
        self.git('commit', '-m', 'baseline')
        self.saved = self.git('rev-parse', 'HEAD').decode().strip()
        self.remote = self.root / 'remote.git'
        self.git('init', '--bare', '-b', 'main', str(self.remote))
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'main')
        self.evidence = self.root / 'test-output.txt'
        self.evidence.write_text('Original fixture command output, not a passed boolean\n')
        self.runner = self.root / 'restart.sh'
        self.runner.write_text('#!/bin/sh\nexec true\n')
        self.retired = self.root / 'already-deleted-runtime'
        self.plan = {
            'schema_version': 1, 'task': 'selected repair', 'repo': str(self.repo),
            'saved_commit': self.saved, 'target_ref': 'origin/main',
            'requirements': ['merge', 'local-main', 'sources', 'restart-dependencies', 'tests'],
            'local_main': {'path': str(self.repo), 'branch': 'main'},
            'sources': [{'path': str(self.repo / 'source.txt'), 'repo_path': 'source.txt'}],
            'restart_dependencies': [{'script': str(self.runner), 'paths': [str(self.retired)]}],
            'evidence': {'tests': self.receipt()},
        }

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, repo=None, input=None):
        return subprocess.check_output(['git', '-C', str(repo or self.repo), *args],
                                       stderr=subprocess.DEVNULL, input=input)

    def receipt(self):
        return {'path': str(self.evidence), 'sha256': hashlib.sha256(self.evidence.read_bytes()).hexdigest()}

    def verify(self):
        return verify_module.verify(self.plan)

    def failed(self, result):
        return {check['object'] for check in result['checks'] if not check['passed']}

    def published_version(self):
        published = self.root / 'published'
        self.git('worktree', 'add', '-b', 'release', str(published))
        (published / 'source.txt').write_text('published implementation\n')
        self.git('add', 'source.txt', repo=published)
        self.git('commit', '-m', 'published', repo=published)
        saved = self.git('rev-parse', 'HEAD', repo=published).decode().strip()
        self.git('push', 'origin', 'HEAD:main', repo=published)
        return published, saved

    def test_complete_uses_actual_commit_files_and_original_main(self):
        result = self.verify()
        self.assertEqual(result['state'], 'complete')
        self.assertIn('git-state-stable', {check['object'] for check in result['checks']})
        self.assertIn('Omitted sources', result['boundary'])

    def test_integrated_project_cannot_omit_development_baseline(self):
        config = self.repo / '.codex/workflow.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'schema_version': 1, 'main_branch': 'main',
                                     'remote': 'origin', 'output_roots': [], 'tests': {}}))
        before = self.git('status', '--porcelain=v1', '-uall')
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertIn('task-lifecycle', self.failed(result))
        self.assertEqual(self.git('status', '--porcelain=v1', '-uall'), before)

    def test_nonexistent_lifecycle_task_does_not_pass(self):
        self.plan['lifecycle_task'] = 'missing-task'
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertIn('task-lifecycle', self.failed(result))

    def test_removing_config_cannot_downgrade_integrated_project(self):
        config = self.repo / '.codex/workflow.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'schema_version': 1, 'main_branch': 'main',
                                     'remote': 'origin', 'output_roots': ['.runtime'], 'tests': {}}))
        subprocess.run([sys.executable, '-B', str(SCRIPT.with_name('task_guard.py')),
                        '--repo', str(self.repo), 'begin', '--task', 'existing-task',
                        '--require', 'cleanup'], check=True, capture_output=True)
        config.unlink()
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertIn('task-lifecycle', self.failed(result))

    def test_clean_published_worktree_cannot_hide_unsynchronized_original_main(self):
        published, saved = self.published_version()
        self.plan.update(repo=str(published), saved_commit=saved,
                         sources=[{'path': str(published / 'source.txt'), 'repo_path': 'source.txt'}])
        self.assertEqual(self.git('status', '--porcelain', repo=published), b'')
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertEqual(self.failed(result), {'local-main'})
        self.git('merge', '--ff-only', 'origin/main')
        self.assertEqual(self.verify()['state'], 'complete')

    def test_other_clean_worktree_cannot_impersonate_main(self):
        published = self.root / 'published'
        self.git('worktree', 'add', '-b', 'release', str(published))
        self.plan['local_main']['path'] = str(published)
        self.assertEqual(self.failed(self.verify()), {'local-main'})

    def test_unrelated_repository_cannot_impersonate_original_main(self):
        clone = self.root / 'unrelated'
        self.git('clone', str(self.remote), str(clone))
        self.plan['local_main']['path'] = str(clone)
        self.assertEqual(self.failed(self.verify()), {'local-main'})

    def test_unsaved_release_input_is_pending_even_with_valid_command_evidence(self):
        release = self.root / 'server-source.txt'
        release.write_text('saved implementation\nmanual server patch\n')
        self.plan['sources'][0]['path'] = str(release)
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertEqual(self.failed(result), {'source:source.txt'})
        (self.repo / 'source.txt').write_bytes(release.read_bytes())
        self.git('add', 'source.txt')
        self.git('commit', '-m', 'save the actual release input')
        self.git('push', 'origin', 'main')
        self.plan['saved_commit'] = self.git('rev-parse', 'HEAD').decode().strip()
        self.assertEqual(self.verify()['state'], 'complete')

    def test_source_mode_and_symlinks_are_not_equivalent(self):
        source = self.repo / 'source.txt'
        source.chmod(0o755)
        self.assertIn('source:source.txt', self.failed(self.verify()))
        source.chmod(0o644)
        linked = self.root / 'linked-source.txt'
        linked.symlink_to(source)
        self.plan['sources'][0]['path'] = str(linked)
        self.assertIn('source:source.txt', self.failed(self.verify()))

    def test_deleted_directory_and_healthy_receipt_do_not_hide_restart_dependency(self):
        self.runner.write_text('#!/bin/sh\nexec node "' + str(self.retired / 'server.mjs') + '"\n')
        self.assertFalse(self.retired.exists())
        self.plan['requirements'].append('cleanup')
        self.plan['cleanup_has_services'] = True
        self.plan['evidence']['cleanup'] = self.receipt()
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertIn('restart:' + str(self.runner), self.failed(result))
        self.assertFalse(self.retired.exists())
        permanent = self.root / 'retained-service' / 'server.mjs'
        permanent.parent.mkdir()
        permanent.write_text('// retained fixture service\n')
        self.runner.write_text('#!/bin/sh\nexec node "' + str(permanent) + '"\n')
        self.assertEqual(self.verify()['state'], 'complete')
        self.assertTrue(permanent.is_file())
        self.assertFalse(self.retired.exists())

    def test_restart_script_is_not_executed_and_prefix_lookalike_is_allowed(self):
        marker = self.root / 'must-not-exist'
        self.runner.write_text('touch "' + str(marker) + '"\nnode "' + str(self.retired) + '-other/server.mjs"\n')
        self.assertEqual(self.verify()['state'], 'complete')
        self.assertFalse(marker.exists())

    def test_missing_restart_script_is_unfinished(self):
        self.runner.unlink()
        self.assertIn('restart:' + str(self.runner), self.failed(self.verify()))

    def test_deploy_automatically_requires_actual_source_comparison(self):
        self.plan['requirements'] = ['deploy']
        self.plan['evidence'] = {'deploy': self.receipt()}
        self.plan.pop('sources')
        result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertIn('sources', self.failed(result))

    def test_cleanup_service_scope_and_restart_inputs_cannot_be_omitted(self):
        self.plan['requirements'] = ['cleanup']
        self.plan['evidence'] = {'cleanup': self.receipt()}
        self.assertIn('cleanup:service-scope', self.failed(self.verify()))
        self.plan['cleanup_has_services'] = True
        self.plan.pop('restart_dependencies')
        self.assertIn('restart-dependencies', self.failed(self.verify()))

    def test_push_requires_actual_named_remote_target(self):
        self.plan['requirements'] = ['push']
        self.plan['evidence'] = {'push': self.receipt()}
        self.assertIn('push:remote-target', self.failed(self.verify()))
        self.plan['remote_target'] = {'remote': 'origin', 'ref': 'refs/heads/main'}
        self.assertEqual(self.verify()['state'], 'complete')

    def test_cli_unqueryable_remote_is_input_error_without_disclosing_its_url(self):
        missing = self.root / 'private-origin-path-must-not-be-printed.git'
        self.git('remote', 'set-url', 'origin', str(missing))
        self.plan['requirements'] = ['push']
        self.plan['evidence'] = {'push': self.receipt()}
        self.plan['remote_target'] = {'remote': 'origin', 'ref': 'refs/heads/main'}
        input_path = self.root / 'unqueryable.json'
        input_path.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--input', str(input_path), '--json'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out['state'], 'input-error')
        self.assertIn('remote=origin ref=refs/heads/main exit=128', out['error'])
        self.assertNotIn(str(missing), result.stdout + result.stderr)
        self.assertNotIn('does not appear to be a git repository', result.stdout + result.stderr)

    def test_cli_queryable_remote_without_matching_branch_remains_pending(self):
        self.plan['requirements'] = ['push']
        self.plan['evidence'] = {'push': self.receipt()}
        self.plan['remote_target'] = {'remote': 'origin', 'ref': 'refs/heads/not-created'}
        input_path = self.root / 'absent-branch.json'
        input_path.write_text(json.dumps(self.plan))
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--input', str(input_path), '--json'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out['state'], 'continue')
        self.assertEqual(self.failed(out), {'push:remote-target'})
        check = next(item for item in out['checks'] if item['object'] == 'push:remote-target')
        self.assertEqual(check['facts']['query_exit'], 2)

    def test_push_cannot_claim_a_saved_candidate_missing_from_the_remote_target(self):
        (self.repo / 'source.txt').write_text('saved but not pushed\n')
        self.git('add', 'source.txt')
        self.git('commit', '-m', 'not pushed')
        self.plan['saved_commit'] = self.git('rev-parse', 'HEAD').decode().strip()
        self.plan['requirements'] = ['push']
        self.plan['evidence'] = {'push': self.receipt()}
        self.plan['remote_target'] = {'remote': 'origin', 'ref': 'refs/heads/main'}
        self.assertEqual(self.failed(self.verify()), {'merge'})

    def test_push_checks_live_remote_instead_of_stale_tracking_ref(self):
        _, _ = self.published_version()
        self.git('update-ref', 'refs/remotes/origin/main', self.saved)
        self.plan['requirements'] = ['push']
        self.plan['evidence'] = {'push': self.receipt()}
        self.plan['remote_target'] = {'remote': 'origin', 'ref': 'refs/heads/main'}
        self.assertIn('push:remote-target', self.failed(self.verify()))

    def test_no_remote_query_without_selected_push(self):
        original = verify_module._git
        def query(repo, *args, **kwargs):
            self.assertNotEqual(args[0], 'ls-remote')
            return original(repo, *args, **kwargs)
        with patch.object(verify_module, '_git', side_effect=query), \
             patch.object(os, 'walk', side_effect=AssertionError('full traversal')):
            self.assertEqual(self.verify()['state'], 'complete')

    def test_unrelated_worktree_files_and_index_bytes_are_preserved(self):
        (self.repo / 'other.txt').write_text('other staged work\n')
        self.git('add', 'other.txt')
        (self.repo / 'other.txt').write_text('other unstaged work\n')
        extra = self.repo / 'untracked.txt'
        extra.write_text('other untracked work\n')
        index = self.repo / '.git/index'
        watched = [self.repo / 'source.txt', self.repo / 'other.txt', extra,
                   index, self.repo / '.git/HEAD', self.repo / '.git/config']
        before = {str(path): path.read_bytes() for path in watched}
        refs = self.git('show-ref')
        self.assertEqual(self.verify()['state'], 'complete')
        self.assertEqual(before, {str(path): path.read_bytes() for path in watched})
        self.assertEqual(refs, self.git('show-ref'))

    def test_target_ref_change_during_check_cannot_be_complete(self):
        other = self.git('commit-tree', 'HEAD^{tree}', '-p', 'HEAD', input=b'concurrent fixture\n').decode().strip()
        original = verify_module._git
        changed = False
        def query(repo, *args, **kwargs):
            nonlocal changed
            result = original(repo, *args, **kwargs)
            if args[:2] == ('cat-file', 'blob') and not changed:
                self.git('update-ref', 'refs/remotes/origin/main', other)
                changed = True
            return result
        with patch.object(verify_module, '_git', side_effect=query):
            result = self.verify()
        self.assertEqual(result['state'], 'continue')
        self.assertIn('git-state-stable', self.failed(result))

    def test_raw_evidence_digest_change_is_pending(self):
        self.evidence.write_text('changed original output\n')
        self.assertIn('evidence:tests', self.failed(self.verify()))

    def test_stop_only_blocks_its_exact_observed_version(self):
        source = self.repo / 'source.txt'
        source.write_text('manual patch\n')
        result = self.verify()
        item = result['unfinished'][0]
        self.plan['stops'] = {item['object']: {
            'kind': 'scope-boundary', 'version': item['version'],
            'attempt': 'Compared actual source to the selected commit',
            'result': 'An unrelated patch needs its own owner',
            'release_condition': 'Owner saves or removes that patch', 'evidence': self.receipt(),
        }}
        self.assertEqual(self.verify()['state'], 'blocked')
        source.write_text('different manual patch\n')
        self.assertEqual(self.verify()['state'], 'continue')

    def test_passed_boolean_and_path_traversal_are_invalid_inputs(self):
        invalid = copy.deepcopy(self.plan)
        invalid['passed'] = True
        with self.assertRaises(ValueError):
            verify_module.verify(invalid)
        self.plan['sources'][0]['repo_path'] = '../source.txt'
        with self.assertRaises(ValueError):
            self.verify()

    def test_cli_complete_continue_blocked_and_input_error_exit_codes(self):
        input_path = self.root / 'request.json'
        def run(expected):
            input_path.write_text(json.dumps(self.plan))
            result = subprocess.run([sys.executable, '-B', str(SCRIPT), '--input', str(input_path), '--json'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(run(0)['state'], 'complete')
        (self.repo / 'source.txt').write_text('pending manual patch\n')
        out = run(3)
        item = out['unfinished'][0]
        self.plan['stops'] = {item['object']: {
            'kind': 'external-dependency', 'version': item['version'],
            'attempt': 'Compared declared source', 'result': 'External owner must save it',
            'release_condition': 'Owner supplies the saved commit', 'evidence': self.receipt(),
        }}
        self.assertEqual(run(1)['state'], 'blocked')
        self.plan['passed'] = True
        self.assertEqual(run(2)['state'], 'input-error')


if __name__ == '__main__':
    unittest.main()
