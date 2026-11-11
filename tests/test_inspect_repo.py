#!/usr/bin/env python3
"""Behavior tests using isolated Git repositories; no real worktree deletion."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import sys

sys.dont_write_bytecode = True

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'inspect_repo.py'
spec = importlib.util.spec_from_file_location('inspect_repo', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shoukou-test-', dir=Path.cwd())
        self.home = Path(self.tmp.name)
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        self.g('init', '-b', 'main')
        self.g('config', 'user.email', 'test@example.invalid')
        self.g('config', 'user.name', 'Test')
        (self.repo / 'tracked.txt').write_text('baseline\n')
        (self.repo / '.gitignore').write_text('.runtime/\n')
        self.g('add', '.')
        self.g('commit', '-m', 'baseline')
        self.wt = self.home / 'candidate with spaces'
        self.g('worktree', 'add', '-b', 'dev/test', str(self.wt))
        self.evidence = self.home / 'evidence.json'

    def tearDown(self):
        self.tmp.cleanup()

    def g(self, *args, repo=None):
        r = subprocess.run(['git', '-C', str(repo or self.repo), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def report(self):
        return m.inventory(self.repo, 'main', True)

    def collect(self, target=None):
        target = target or self.wt
        s = m.snapshot(target)
        r = self.report()
        e = {'schema_version': 1, 'observed_at_unix': time.time(),
             'target': {'path': s['path'], 'head': s['head'], 'branch': s['branch'], 'fingerprint': s['fingerprint'],
                        'base_ref': r['base_ref'], 'base_head': r['base_head']},
             'cleanup_authorized': True, 'task_delivery': {'accepted': True, 'head': s['head']},
             'app': {'source': 'isolated fixture app query', 'complete': True,
                     'chats': [{'id': 'owner', 'cwd': str(self.repo), 'archived': False}]},
             'processes': {'source': 'isolated fixture process query', 'complete': True, 'active': []},
             'ignored': {}}
        return e

    def check(self, e=None, target=None, report=None):
        self.evidence.write_text(json.dumps(e or self.collect(target)))
        return m.cleanup_check(self.repo, report or self.report(), target or self.wt, self.evidence)

    def blocked(self, reason, e=None, target=None):
        r = self.check(e, target)
        self.assertEqual(r['cleanup_preflight'], 'blocked')
        self.assertIn(reason, r['blockers'])
        self.assertTrue(self.wt.exists())

    def test_clean_secondary_passes_without_mutation(self):
        before = self.g('worktree', 'list', '--porcelain')
        self.assertEqual(self.check()['cleanup_preflight'], 'passed')
        self.assertEqual(before, self.g('worktree', 'list', '--porcelain'))
        self.assertTrue(self.wt.exists())

    def test_summary_bounds_ignored_output_and_keeps_dirty_candidate(self):
        self.ignored_file()
        (self.wt / 'feature.txt').write_text('unsubmitted feature')
        full = self.report()
        concise = m.summary(full)
        candidate = concise['worktrees'][1]
        self.assertEqual(candidate['ignored_count'], 1)
        self.assertNotIn('ignored', candidate)
        self.assertNotIn('ignored_metadata', candidate)
        self.assertIn('feature.txt', candidate['changes']['untracked'])
        self.assertEqual(candidate['delivery'], 'uncommitted-work-remains')
        self.assertEqual(len(concise['branches']), len(full['branches']))
        self.assertIn('ignored', full['worktrees'][1])

    def test_target_inventory_does_not_snapshot_unrelated_directories(self):
        with patch.object(m, 'snapshot', wraps=m.snapshot) as query:
            report = m.inventory(self.repo, 'main', True, target=self.wt)
        self.assertEqual([s['path'] for s in report['worktrees']], [str(self.wt)])
        self.assertEqual(query.call_count, 1)
        self.assertEqual(Path(query.call_args.args[0]), self.wt)

    def test_storage_counts_link_itself_and_deduplicates_hardlinks(self):
        import os
        directory = self.wt / '.runtime'
        directory.mkdir()
        outside = self.home / 'outside.bin'
        outside.write_bytes(b'x' * 16384)
        (directory / 'link').symlink_to(outside)
        data = directory / 'cache.bin'
        data.write_bytes(b'x' * 8192)
        os.link(data, directory / 'same-data.bin')
        state = m.snapshot(self.wt)
        usage = state['ignored_usage']
        link = (directory / 'link').lstat()
        self.assertEqual(usage['logical_bytes'], 16384 + link.st_size)
        self.assertEqual(usage['allocated_bytes'], data.stat().st_blocks * 512 + link.st_blocks * 512)
        self.assertEqual(usage['file_count'], 3)
        self.assertIn('available_bytes', state['filesystem'])
        self.assertEqual(outside.stat().st_size, 16384)

    def test_baseline_in_main_does_not_deliver_uncommitted_feature(self):
        (self.wt / 'feature.txt').write_text('unsubmitted source')
        r = self.report()['worktrees'][1]
        self.assertEqual(r['committed_merge'], 'committed-head-in-base')
        self.assertEqual(r['delivery'], 'uncommitted-work-remains')
        self.blocked('staged-unstaged-or-untracked-work-remains')

    def test_staged_changes_block(self):
        (self.wt / 'tracked.txt').write_text('new')
        self.g('add', 'tracked.txt', repo=self.wt)
        self.blocked('staged-unstaged-or-untracked-work-remains')

    def test_unstaged_changes_block(self):
        (self.wt / 'tracked.txt').write_text('new')
        self.blocked('staged-unstaged-or-untracked-work-remains')

    def test_primary_checkout_blocks(self):
        self.blocked('primary-checkout-must-be-retained', target=self.repo)

    def test_new_commit_not_merged_blocks(self):
        (self.wt / 'tracked.txt').write_text('new')
        self.g('commit', '-am', 'feature', repo=self.wt)
        self.blocked('current-committed-head-not-proven-merged')

    def squash(self):
        old = self.g('rev-parse', 'HEAD', repo=self.wt)
        (self.wt / 'tracked.txt').write_text('new')
        self.g('commit', '-am', 'feature', repo=self.wt)
        head = self.g('rev-parse', 'HEAD', repo=self.wt)
        self.g('merge', '--squash', 'dev/test')
        self.g('commit', '-m', 'squashed feature')
        merge = self.g('rev-parse', 'HEAD')
        return old, head, merge

    def test_exact_squash_pr_proof_passes(self):
        _, head, merge = self.squash()
        pr = {'state': 'MERGED', 'headRefName': 'dev/test', 'headRefOid': head,
              'baseRefName': 'main', 'mergeCommit': {'oid': merge}}
        r = self.report()
        r['github']['prs'] = [pr]
        result = self.check(report=r)
        self.assertEqual(result['cleanup_preflight'], 'passed')
        self.assertEqual(result['committed_merge'], 'exact-head-merged-via-pr')

    def test_old_same_name_pr_does_not_cover_new_commit(self):
        old, head, merge = self.squash()
        pr = {'state': 'MERGED', 'headRefName': 'dev/test', 'headRefOid': old,
              'baseRefName': 'main', 'mergeCommit': {'oid': merge}}
        self.assertEqual(m.merged_proof(self.repo, 'dev/test', head, 'main', merge, [pr]), 'not-proven-in-base')

    def test_wrong_base_or_unavailable_merge_commit_does_not_prove_pr(self):
        _, head, merge = self.squash()
        for base, oid in [('other', merge), ('main', 'a' * 40)]:
            pr = {'state': 'MERGED', 'headRefName': 'dev/test', 'headRefOid': head,
                  'baseRefName': base, 'mergeCommit': {'oid': oid}}
            self.assertEqual(m.merged_proof(self.repo, 'dev/test', head, 'main', merge, [pr]), 'not-proven-in-base')

    def test_recent_list_is_not_complete_query(self):
        e = self.collect()
        e['app']['complete'] = False
        self.blocked('app-occupancy-query-incomplete', e)

    def native_evidence(self):
        e = self.collect()
        e['app'].update(mode='native-archive', complete=False, archive={
            'tool': 'mcp__codex_app__archive_worktree', 'identity_key': 'fixture-worktree-key',
            'target_path': str(self.wt), 'owner_thread_id': 'owner', 'current_thread_id': 'owner',
            'contract_source': 'fixture of enabled tool contract rejecting primary, pinned and shared worktrees',
            'attachment_source': 'fixture of owner list_artifacts response'})
        return e

    def test_native_guard_allows_incomplete_list_only_for_exact_native_action(self):
        before = self.g('worktree', 'list', '--porcelain')
        result = self.check(self.native_evidence())
        self.assertEqual(result['cleanup_preflight'], 'native-archive-ready')
        self.assertEqual(result['required_native_action'], {
            'tool': 'mcp__codex_app__archive_worktree', 'root': 'fixture-worktree-key', 'owner_thread_id': 'owner'})
        self.assertEqual(before, self.g('worktree', 'list', '--porcelain'))
        self.assertTrue(self.wt.exists())

    def test_native_mode_missing_binding_blocks(self):
        e = self.native_evidence()
        del e['app']['archive']
        self.blocked('native-archive-binding-missing', e)

    def test_native_wrong_target_owner_tool_or_missing_source_blocks(self):
        for field, val in [('target_path', str(self.repo)), ('owner_thread_id', 'other'),
                           ('current_thread_id', 'other'), ('tool', 'git-worktree-remove'),
                           ('identity_key', ''), ('contract_source', ''), ('attachment_source', '')]:
            e = self.native_evidence()
            e['app']['archive'][field] = val
            self.blocked('native-archive-binding-invalid', e)

    def test_native_owner_must_be_in_observed_continuing_chats(self):
        e = self.native_evidence()
        e['app']['chats'] = []
        self.blocked('native-archive-binding-invalid', e)

    def test_native_guard_does_not_override_known_idle_chat_use(self):
        e = self.native_evidence()
        e['app']['chats'].append({'id': 'another-idle-chat', 'cwd': str(self.wt), 'archived': False})
        self.blocked('continuing-chat-has-target-or-missing-directory', e)

    def test_native_guard_does_not_override_process_occupancy(self):
        e = self.native_evidence()
        e['processes']['active'] = [123]
        self.blocked('process-query-incomplete-or-target-in-use', e)

    def test_native_guard_does_not_override_uncommitted_source(self):
        (self.wt / 'feature.txt').write_text('unsubmitted source')
        self.blocked('staged-unstaged-or-untracked-work-remains', self.native_evidence())

    def test_native_guard_does_not_override_unmerged_head(self):
        (self.wt / 'tracked.txt').write_text('new feature')
        self.g('commit', '-am', 'not merged', repo=self.wt)
        self.blocked('current-committed-head-not-proven-merged', self.native_evidence())

    def test_unknown_app_mode_never_falls_back_to_pass(self):
        e = self.collect()
        e['app']['mode'] = 'unknown'
        self.blocked('unsupported-app-check-mode', e)

    def test_native_guard_keeps_backup_content_validation(self):
        src = self.ignored_file()
        backup = self.home / 'incorrect-backup.txt'
        backup.write_text('wrong data')
        e = self.native_evidence()
        e['ignored'][str(src.relative_to(self.wt))] = {
            'action': 'preserve', 'reason': 'required evidence', 'backup': str(backup),
            'sha256': m.file_digest(src)}
        self.blocked('backup-content-mismatch', e)

    def test_native_ready_cli_returns_zero_and_does_not_delete(self):
        self.evidence.write_text(json.dumps(self.native_evidence()))
        r = subprocess.run([sys.executable, str(SCRIPT), '--repo', str(self.repo), '--base', 'main',
                            '--offline', '--check-cleanup', str(self.wt), '--evidence', str(self.evidence)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['cleanup_preflight'], 'native-archive-ready')
        self.assertTrue(self.wt.is_dir())

    def test_large_classification_keeps_exact_files_and_directory_cache(self):
        names = ['evidence/' + str(i) + '.log' for i in range(2000)]
        dispositions = {name: {'action': 'preserve', 'reason': 'evidence'} for name in names}
        names.extend('node_modules/pkg/' + str(i) + '.js' for i in range(20000))
        dispositions['node_modules/'] = {'action': 'discard', 'reason': 'rebuildable dependencies'}
        result = m.classify_ignored(names, dispositions)
        self.assertEqual(len(result), len(names))
        self.assertEqual(result['evidence/100.log']['action'], 'preserve')
        self.assertEqual(result['node_modules/pkg/100.js']['action'], 'discard')
        dispositions['node_modules/pkg/'] = {'action': 'discard', 'reason': 'overlapping cache'}
        self.assertIsNone(m.classify_ignored(names, dispositions))

    def test_invalid_or_unmatched_classification_rules_still_fail(self):
        for key in ['/absolute', '../parent', 'unused/', 42]:
            self.assertIsNone(m.classify_ignored(['evidence.txt'], {
                'evidence.txt': {'action': 'preserve'}, key: {'action': 'discard'}}))

    def test_missing_app_evidence_blocks(self):
        e = self.collect()
        del e['app']
        self.blocked('app-occupancy-query-incomplete', e)

    def test_idle_chat_still_using_target_blocks(self):
        e = self.collect()
        e['app']['chats'][0]['cwd'] = str(self.wt)
        self.blocked('continuing-chat-has-target-or-missing-directory', e)

    def test_continuing_chat_missing_destination_blocks(self):
        e = self.collect()
        e['app']['chats'][0]['cwd'] = str(self.home / 'missing')
        self.blocked('continuing-chat-has-target-or-missing-directory', e)

    def test_archived_history_can_keep_old_path(self):
        e = self.collect()
        e['app']['chats'][0].update(cwd=str(self.wt), archived=True)
        self.assertEqual(self.check(e)['cleanup_preflight'], 'passed')

    def test_active_process_blocks(self):
        e = self.collect()
        e['processes']['active'] = [123]
        self.blocked('process-query-incomplete-or-target-in-use', e)

    def test_stale_evidence_and_no_authorization_block(self):
        e = self.collect()
        e['observed_at_unix'] -= 121
        e['cleanup_authorized'] = False
        r = self.check(e)
        self.assertIn('evidence-missing-stale-or-future', r['blockers'])
        self.assertIn('cleanup-not-authorized', r['blockers'])

    def test_acceptance_must_match_head(self):
        e = self.collect()
        e['task_delivery']['head'] = '0' * 40
        self.blocked('task-acceptance-not-bound-to-current-head', e)

    def ignored_file(self):
        (self.wt / '.runtime').mkdir()
        f = self.wt / '.runtime' / 'evidence.txt'
        f.write_text('valuable ignored evidence')
        return f

    def test_ignored_file_requires_disposition(self):
        self.ignored_file()
        self.blocked('ignored-files-not-fully-classified')

    def test_verified_external_backup_passes_wrong_backup_blocks(self):
        f = self.ignored_file()
        backup = self.home / 'backup.txt'
        backup.write_bytes(f.read_bytes())
        e = self.collect()
        e['ignored'][str(f.relative_to(self.wt))] = {'action': 'preserve', 'reason': 'acceptance log',
                                                  'backup': str(backup), 'sha256': m.hashlib.sha256(f.read_bytes()).hexdigest()}
        self.assertEqual(self.check(e)['cleanup_preflight'], 'passed')
        backup.write_text('corrupt backup')
        self.blocked('backup-content-mismatch', e)

    def test_backup_inside_target_blocks(self):
        f = self.ignored_file()
        e = self.collect()
        e['ignored'][str(f.relative_to(self.wt))] = {'action': 'preserve', 'reason': 'acceptance log',
                                                  'backup': str(f), 'sha256': m.hashlib.sha256(f.read_bytes()).hexdigest()}
        self.blocked('backup-unusable-or-inside-target', e)

    def test_directory_discard_covers_only_explicit_group(self):
        self.ignored_file()
        e = self.collect()
        e['ignored'] = {'.runtime/': {'action': 'discard', 'reason': 'fixture disposable cache'}}
        self.assertEqual(self.check(e)['cleanup_preflight'], 'passed')
        e['ignored']['.runtime/evidence.txt'] = {'action': 'discard', 'reason': 'overlapping rule'}
        self.blocked('ignored-files-not-fully-classified', e)

    def test_directory_preservation_requires_individual_copies(self):
        self.ignored_file()
        e = self.collect()
        e['ignored'] = {'.runtime/': {'action': 'preserve', 'reason': 'logs', 'backup': str(self.home)}}
        self.blocked('ignored-files-not-fully-classified', e)

    def test_symlink_backup_is_not_a_verified_copy(self):
        f = self.ignored_file()
        backup = self.home / 'symlink.txt'
        backup.symlink_to(f)
        e = self.collect()
        e['ignored'][str(f.relative_to(self.wt))] = {'action': 'preserve', 'reason': 'acceptance log',
                                                  'backup': str(backup), 'sha256': m.hashlib.sha256(f.read_bytes()).hexdigest()}
        self.blocked('backup-unusable-or-inside-target', e)

    def test_new_ignored_file_invalidates_evidence(self):
        e = self.collect()
        self.ignored_file()
        self.blocked('target-or-base-changed-since-evidence', e)

    def test_base_change_invalidates_evidence(self):
        e = self.collect()
        (self.repo / 'later.txt').write_text('later base commit')
        self.g('add', 'later.txt')
        self.g('commit', '-m', 'later')
        self.blocked('target-or-base-changed-since-evidence', e)

    def test_nested_repository_blocks_even_when_ignored(self):
        (self.wt / '.runtime').mkdir()
        self.g('init', str(self.wt / '.runtime' / 'nested'))
        self.blocked('nested-repository-or-submodule-needs-separate-preservation')

    def test_cli_blocked_nonzero_never_removes_target(self):
        e = self.collect()
        e['app']['complete'] = False
        self.evidence.write_text(json.dumps(e))
        r = subprocess.run(['bash', str(SCRIPT.with_name('inspect-repo.sh')), '--repo', str(self.repo),
                            '--base', 'main', '--offline', '--check-cleanup', str(self.wt),
                            '--evidence', str(self.evidence)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(r.stdout)['cleanup_preflight'], 'blocked')
        self.assertTrue(self.wt.exists())

    def test_invalid_evidence_cli_returns_error(self):
        self.evidence.write_text('{invalid')
        r = subprocess.run(['python3', str(SCRIPT), '--repo', str(self.repo), '--base', 'main', '--offline',
                            '--check-cleanup', str(self.wt), '--evidence', str(self.evidence)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(self.wt.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
