#!/usr/bin/env python3
"""Final outcome checks against isolated local and bare remote repositories."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import verify_closeout as verifier


class FinalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shoukou-final-', dir=Path.cwd())
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.remote = self.root / 'origin.git'
        self.g('init', '-b', 'main', str(self.repo), cwd=self.root)
        self.g('init', '--bare', str(self.remote))
        self.g('config', 'user.name', 'Test')
        self.g('config', 'user.email', 'test@example.invalid')
        (self.repo / 'source.txt').write_text('base\n')
        (self.repo / '.gitignore').write_text('.runtime/\ncache/\nnode_modules/\n')
        self.g('add', '.')
        self.g('commit', '-m', 'base')
        self.g('remote', 'add', 'origin', str(self.remote))
        self.g('push', '-u', 'origin', 'main')
        self.head = self.g('rev-parse', 'HEAD')
        self.g('branch', 'dev/task')
        self.g('push', 'origin', 'dev/task')
        self.ledger = {'schema_version': 1, 'mode': 'full', 'scope': 'all-task-branches',
                       'base': {'ref': 'origin/main', 'branch': 'main', 'head': self.head},
                       'main_path': str(self.repo), 'directories': [],
                       'candidates': [{'branch': 'dev/task',
                                       'local': {'head': self.head, 'outcome': 'removed'},
                                       'remote': {'head': self.head, 'outcome': 'removed'}}]}
        self.capture_runtime()
        self.capture_branch_inventory()

    def capture_branch_inventory(self):
        before = self.root / 'branches-before.json'
        common = Path(self.g('rev-parse', '--git-common-dir'))
        common = (self.repo / common).resolve() if not common.is_absolute() else common.resolve()
        local = dict(line.split('|') for line in self.g('for-each-ref', '--format=%(refname:short)|%(objectname)', 'refs/heads').splitlines())
        remote = {} if not self.g('remote') else {
            line.split()[1].removeprefix('refs/heads/'): line.split()[0]
            for line in self.g('ls-remote', '--heads', 'origin').splitlines()}
        before.write_text(json.dumps({'schema_version': 1, 'git_common_dir': str(common),
                                      'observed_at_unix': time.time(), 'local': local, 'remote': remote}))
        self.ledger['branch_inventory'] = {'before_inventory': str(before),
                                         'before_sha256': hashlib.sha256(before.read_bytes()).hexdigest(),
                                         'introduced': {'local': {}, 'remote': {}}}

    def capture_runtime(self, classifications=None):
        before = self.root / 'before-inventory.json'
        before.write_text(json.dumps(verifier.git_state.inventory(self.repo, self.ledger['base']['ref'], True)))
        roots = [self.repo, *(Path(item['path']) for item in self.ledger['directories'])]
        self.ledger['runtime'] = {
            'before_inventory': str(before), 'before_sha256': hashlib.sha256(before.read_bytes()).hexdigest(),
            'classifications': classifications or {str(root): {} for root in roots},
            'services': {'source': 'isolated fixture service query', 'complete': True,
                         'observed_at_unix': time.time(), 'items': []}}

    def tearDown(self):
        self.tmp.cleanup()

    def g(self, *args, cwd=None):
        r = subprocess.run(['git', '-C', str(cwd or self.repo), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def remove_refs(self):
        self.g('branch', '-d', 'dev/task')
        self.g('push', 'origin', '--delete', 'dev/task')

    def verify(self, ledger=None):
        return verifier.verify(self.repo, ledger or self.ledger)

    def test_merge_without_ref_cleanup_fails(self):
        result = self.verify()
        self.assertEqual(result['closeout'], 'blocked')
        self.assertIn('local:dev/task:ref-still-exists', result['failures'])
        self.assertIn('remote:dev/task:ref-still-exists', result['failures'])

    def test_both_refs_removed_passes_without_mutation(self):
        self.remove_refs()
        before = self.g('status', '--porcelain')
        self.assertEqual(self.verify()['closeout'], 'complete')
        self.assertEqual(before, self.g('status', '--porcelain'))
        self.assertTrue(self.repo.exists())

    def test_remote_deletion_does_not_imply_local_deletion(self):
        self.g('push', 'origin', '--delete', 'dev/task')
        self.assertIn('local:dev/task:ref-still-exists', self.verify()['failures'])

    def test_declared_blocked_retention_is_partial(self):
        self.g('push', 'origin', '--delete', 'dev/task')
        self.ledger['candidates'][0]['local'].update(
            outcome='retained', retention='blocked', reason='Native Git query failed',
            review=self.retention_review())
        self.assertEqual(self.verify()['closeout'], 'partial')

    def test_unknown_template_requires_continued_investigation(self):
        review = self.retention_review('unknown', None, 'ownership')
        review['delivery'].update(accepted=None, uncommitted='unknown')
        self.retained_local(review)
        result = self.verify()
        self.assertEqual(result['execution']['state'], 'continue')
        self.assertIn('local:dev/task', [x['object'] for x in result['execution']['unfinished']])
        self.assertEqual(self.g('rev-parse', 'dev/task'), self.head)

    def paused_local(self):
        self.retained_local(self.retention_review('paused', None, 'ownership'))
        record = self.root / 'original-pause.txt'
        record.write_text('Fixture user paused this exact task; preserve source and data.')
        self.ledger['stops'] = {'local:dev/task': {
            'version': self.head, 'kind': 'user-pause', 'attempt': 'Read the original task pause record',
            'result': 'Implementation paused; unfinished work must remain in place',
            'release_condition': 'User resumes this task',
            'evidence': {'path': str(record), 'sha256': hashlib.sha256(record.read_bytes()).hexdigest()}}}

    def test_real_pause_can_stop_only_its_bound_object(self):
        self.paused_local()
        result = self.verify()
        self.assertEqual(result['closeout'], 'partial')
        self.assertEqual(result['execution']['state'], 'blocked')
        self.assertEqual(result['execution']['unfinished'], [])
        self.assertEqual(result['execution']['blocked'][0]['object'], 'local:dev/task')
        self.assertEqual(self.g('rev-parse', 'dev/task'), self.head)
        ledger = self.root / 'paused-ledger.json'
        ledger.write_text(json.dumps(self.ledger))
        cli = subprocess.run([sys.executable, verifier.__file__, '--repo', str(self.repo),
                              '--ledger', str(ledger)], capture_output=True, text=True)
        self.assertEqual(cli.returncode, 1, cli.stdout)
        self.assertEqual(json.loads(cli.stdout)['execution']['state'], 'blocked')

    def test_missing_runtime_cannot_be_hidden_by_a_valid_branch_pause(self):
        self.paused_local()
        del self.ledger['runtime']
        result = self.verify()
        self.assertEqual(result['execution']['state'], 'continue')
        self.assertIn('runtime-resources-not-assessed', [x['object'] for x in result['execution']['unfinished']])

    def test_stale_stop_evidence_cannot_end_the_investigation(self):
        self.paused_local()
        Path(self.ledger['stops']['local:dev/task']['evidence']['path']).write_text('Task resumed')
        result = self.verify()
        self.assertEqual(result['execution']['state'], 'continue')
        self.assertEqual(result['execution']['blocked'], [])

    def test_clean_delivery_with_no_remaining_work_is_execution_complete(self):
        self.remove_refs()
        self.assertEqual(self.verify()['execution']['state'], 'complete')

    def retention_review(self, state='completed', required=False, kind='tool'):
        review = {
            'head': self.head,
            'task': {'state': state, 'source': 'Fixture task completion/user instruction'},
            'delivery': {'accepted': True, 'uncommitted': 'none',
                         'source': 'Fixture exact-version acceptance and file inventory'},
            'dependency': {'required': required, 'source': 'Fixture branch-specific dependency query'},
            'blocker': {'kind': kind, 'source': 'Fixture original query error',
                        'finding': 'Git query unavailable during the original attempt',
                        'next_action': 'Retry the branch-specific Git query',
                        'execution_location': str(self.repo),
                        'release_condition': 'Fresh branch check succeeds'}}
        return review

    def retained_local(self, review=None, retention='blocked'):
        self.g('push', 'origin', '--delete', 'dev/task')
        item = self.ledger['candidates'][0]['local']
        item.update(outcome='retained', retention=retention, reason='Fixture retention decision')
        if review is not None:
            item['review'] = review
        return item

    def test_existing_branch_cannot_finish_with_only_a_retention_reason(self):
        self.retained_local(retention='agreed')
        self.assertIn('local:dev/task:retained-branch-review-missing', self.verify()['failures'])

    def test_directory_problem_is_not_a_branch_blocker(self):
        self.retained_local(self.retention_review(kind='directory'))
        result = self.verify()
        self.assertIn('local:dev/task:directory-only-branch-blocker', result['failures'])
        self.assertTrue(self.repo.is_dir())
        self.assertEqual(self.g('rev-parse', 'dev/task'), self.head)

    def test_old_branch_task_state_cannot_be_relabelled_agreed(self):
        for state in ('completed', 'unknown'):
            with self.subTest(state=state):
                self.ledger['candidates'][0]['local'].update(
                    outcome='retained', retention='agreed', reason='Keep old environment',
                    review=self.retention_review(state=state))
                self.assertIn('local:dev/task:retained-task-disposition-inconsistent', self.verify()['failures'])

    def test_every_blocker_needs_investigation_and_release_details(self):
        item = self.retained_local(self.retention_review())
        for field in ('source', 'finding', 'next_action', 'execution_location', 'release_condition'):
            with self.subTest(field=field):
                item['review'] = self.retention_review()
                del item['review']['blocker'][field]
                self.assertIn('local:dev/task:branch-blocker-details-missing', self.verify()['failures'])

    def test_review_is_bound_to_the_exact_retained_head(self):
        review = self.retention_review()
        review['head'] = '0' * 40
        self.retained_local(review)
        self.assertIn('local:dev/task:retained-review-head-mismatch', self.verify()['failures'])

    def test_task_delivery_and_branch_dependency_each_need_sources(self):
        item = self.retained_local(self.retention_review())
        for part in ('task', 'delivery', 'dependency'):
            with self.subTest(part=part):
                item['review'] = self.retention_review()
                item['review'][part]['source'] = '  '
                self.assertIn('local:dev/task:retained-' + part + '-review-invalid', self.verify()['failures'])

    def test_uncommitted_work_cannot_be_declared_absent(self):
        self.retained_local(self.retention_review())
        wt = self.root / 'dirty-task'
        self.g('worktree', 'add', str(wt), 'dev/task')
        (wt / 'source.txt').write_text('unique unfinished work\n')
        result = self.verify()
        self.assertIn('local:dev/task:retained-uncommitted-state-contradicts-worktree', result['failures'])
        self.assertEqual((wt / 'source.txt').read_text(), 'unique unfinished work\n')

    def test_resolved_delivery_blocker_requires_further_branch_handling(self):
        self.retained_local(self.retention_review(kind='delivery'))
        self.assertIn('local:dev/task:branch-retention-blocker-already-resolved', self.verify()['failures'])

    def test_failed_acceptance_is_a_real_partial_blocker(self):
        review = self.retention_review(kind='acceptance')
        review['delivery']['accepted'] = False
        self.retained_local(review)
        result = self.verify()
        self.assertEqual(result['closeout'], 'partial')
        self.assertTrue(result['refs'][0]['review']['head_in_base'])

    def test_existing_continuing_and_long_lived_branches_are_protected(self):
        item = self.retained_local()
        for state in ('continuing', 'long-lived'):
            with self.subTest(state=state):
                item.update(retention='agreed', review=self.retention_review(state, True))
                self.assertEqual(self.verify()['closeout'], 'complete')
        self.assertEqual(self.g('rev-parse', 'dev/task'), self.head)

    def test_unknown_dependency_cannot_be_rewritten_as_agreed(self):
        self.retained_local(self.retention_review('continuing', None), retention='agreed')
        self.assertIn('local:dev/task:retained-task-disposition-inconsistent', self.verify()['failures'])

    def test_remote_retention_needs_its_own_exact_head_review(self):
        self.g('branch', '-d', 'dev/task')
        item = self.ledger['candidates'][0]['remote']
        item.update(outcome='retained', retention='agreed', reason='Remote task continues')
        self.assertIn('remote:dev/task:retained-branch-review-missing', self.verify()['failures'])
        item['review'] = self.retention_review('continuing', True)
        self.assertEqual(self.verify()['closeout'], 'complete')

    def test_explicit_stage_retention_is_verifiable(self):
        self.ledger['mode'] = 'stage'
        for location in ('local', 'remote'):
            self.ledger['candidates'][0][location].update(outcome='retained', retention='agreed', reason='Task continues by user agreement')
        self.assertEqual(self.verify()['closeout'], 'complete')

    def test_unexplained_retention_fails(self):
        self.ledger['candidates'][0]['local']['outcome'] = 'retained'
        self.assertIn('local:dev/task:retention-not-explained', self.verify()['failures'])

    def test_remote_new_commit_is_observed_without_fetch(self):
        self.ledger['candidates'][0]['remote'].update(outcome='retained', retention='blocked', reason='Pending work')
        self.g('checkout', 'dev/task')
        (self.repo / 'source.txt').write_text('new\n')
        self.g('commit', '-am', 'new remote head')
        self.g('push', 'origin', 'dev/task')
        self.assertIn('remote:dev/task:retained-head-changed-or-missing', self.verify()['failures'])

    def test_main_remote_advance_blocks_stale_local_baseline(self):
        self.remove_refs()
        (self.repo / 'source.txt').write_text('new main\n')
        self.g('commit', '-am', 'main advance')
        self.g('push', 'origin', 'main')
        self.assertIn('base-does-not-match-ledger-and-live-origin', self.verify()['failures'])

    def test_unsynchronized_main_fails(self):
        self.remove_refs()
        (self.repo / 'source.txt').write_text('local ahead\n')
        self.g('commit', '-am', 'local only')
        self.assertIn('local-main-not-synchronized', self.verify()['failures'])

    def test_dirty_main_fails(self):
        self.remove_refs()
        (self.repo / 'untracked.txt').write_text('unaccounted work')
        self.assertIn('main-has-unaccounted-changes', self.verify()['failures'])

    def test_new_branch_is_not_silently_omitted_from_all_branch_audit(self):
        self.remove_refs()
        self.g('branch', 'dev/new-task')
        self.assertIn('local:dev/new-task:candidate-not-accounted-for', self.verify()['failures'])

    def new_branch(self, state='continuing', retention='agreed'):
        self.g('branch', 'dev/new-task')
        item = {'branch': 'dev/new-task',
                'local': {'head': self.head, 'outcome': 'retained', 'retention': retention,
                          'reason': 'New task observed during closeout',
                          'review': self.retention_review(state, True if state == 'continuing' else None)},
                'remote': {'head': None, 'outcome': 'absent'}}
        self.ledger['candidates'].append(item)
        self.ledger['branch_inventory']['introduced']['local']['dev/new-task'] = {
            'head': self.head, 'state': state, 'source': 'fixture task observation'}
        return item

    def test_new_branch_needs_explicit_classification_even_when_in_ledger(self):
        self.remove_refs()
        self.new_branch()
        self.ledger['branch_inventory']['introduced']['local'].clear()
        self.assertIn('local:dev/new-task:new-branch-not-reviewed', self.verify()['failures'])

    def test_new_continuing_branch_can_be_protected_and_reported_separately(self):
        self.remove_refs()
        self.new_branch()
        result = self.verify()
        self.assertEqual(result['closeout'], 'complete')
        self.assertEqual(result['branch_changes']['introduced']['local'], ['dev/new-task'])

    def test_new_unknown_branch_cannot_be_agreed_retention(self):
        self.remove_refs()
        self.new_branch(state='unknown')
        self.assertIn('local:dev/new-task:new-branch-disposition-inconsistent', self.verify()['failures'])

    def test_new_unknown_branch_is_partial_when_preserved_as_blocked(self):
        self.remove_refs()
        self.new_branch(state='unknown', retention='blocked')
        self.assertEqual(self.verify()['closeout'], 'partial')

    def test_new_paused_branch_with_unresolved_dependencies_stays_protected(self):
        self.remove_refs()
        self.new_branch(state='paused', retention='blocked')
        result = self.verify()
        self.assertEqual(result['closeout'], 'partial')
        self.assertEqual(result['execution']['state'], 'continue')
        self.assertEqual(self.g('rev-parse', 'dev/new-task'), self.head)

    def test_new_completed_branch_can_be_removed_after_separate_review(self):
        self.remove_refs()
        item = self.new_branch(state='completed')
        self.g('branch', '-d', 'dev/new-task')
        item['local'] = {'head': self.head, 'outcome': 'removed'}
        self.assertEqual(self.verify()['closeout'], 'complete')

    def test_new_branch_review_is_bound_to_observed_head(self):
        self.remove_refs()
        self.new_branch()
        self.ledger['branch_inventory']['introduced']['local']['dev/new-task']['head'] = '0' * 40
        self.assertIn('local:dev/new-task:new-branch-review-head-mismatch', self.verify()['failures'])

    def test_initial_removed_branch_cannot_disappear_from_ledger(self):
        self.remove_refs()
        self.ledger['candidates'] = []
        self.assertIn('local:dev/task:initial-branch-not-accounted-for', self.verify()['failures'])

    def test_initial_existing_branch_cannot_be_relabelled_originally_absent(self):
        self.remove_refs()
        self.ledger['candidates'][0]['local'] = {'head': None, 'outcome': 'absent'}
        self.assertIn('local:dev/task:initial-branch-cannot-be-declared-absent', self.verify()['failures'])

    def test_new_removed_branch_requires_a_full_head_id(self):
        self.remove_refs()
        item = self.new_branch(state='completed')
        self.g('branch', '-d', 'dev/new-task')
        item['local'] = {'head': 'not-an-object-id', 'outcome': 'removed'}
        self.ledger['branch_inventory']['introduced']['local']['dev/new-task']['head'] = 'not-an-object-id'
        self.assertIn('local:dev/new-task:new-branch-review-head-mismatch', self.verify()['failures'])

    def test_missing_initial_branch_inventory_is_not_complete(self):
        self.remove_refs()
        del self.ledger['branch_inventory']
        self.assertEqual(self.verify()['closeout'], 'partial')

    def test_tampered_initial_branch_inventory_is_rejected(self):
        self.remove_refs()
        Path(self.ledger['branch_inventory']['before_inventory']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'branch inventory checksum'):
            self.verify()

    def test_branch_created_during_final_verification_cannot_slip_through(self):
        self.remove_refs()
        real_verify = verifier.runtime_resources.verify
        def concurrent_task(*args):
            result = real_verify(*args)
            self.g('branch', 'dev/late-task')
            return result
        with patch.object(verifier.runtime_resources, 'verify', side_effect=concurrent_task):
            self.assertIn('branch-refs-changed-during-final-verification', self.verify()['failures'])

    def test_remote_only_new_branch_requires_its_own_review(self):
        self.remove_refs()
        self.g('push', 'origin', 'main:refs/heads/dev/remote-task')
        self.ledger['candidates'].append({'branch': 'dev/remote-task',
                                          'local': {'head': None, 'outcome': 'absent'},
                                          'remote': {'head': self.head, 'outcome': 'retained',
                                                     'retention': 'agreed', 'reason': 'New remote task continues',
                                                     'review': self.retention_review('continuing', True)}})
        self.assertIn('remote:dev/remote-task:new-branch-not-reviewed', self.verify()['failures'])
        self.ledger['branch_inventory']['introduced']['remote']['dev/remote-task'] = {
            'head': self.head, 'state': 'continuing', 'source': 'fixture remote task record'}
        self.assertEqual(self.verify()['closeout'], 'complete')

    def test_remote_branch_created_during_final_verification_blocks(self):
        self.remove_refs()
        real_verify = verifier.runtime_resources.verify
        def concurrent_task(*args):
            result = real_verify(*args)
            self.g('push', 'origin', 'main:refs/heads/dev/late-remote')
            return result
        with patch.object(verifier.runtime_resources, 'verify', side_effect=concurrent_task):
            self.assertIn('branch-refs-changed-during-final-verification', self.verify()['failures'])

    def test_capture_branches_cli_records_local_and_real_remote_heads(self):
        self.g('push', 'origin', 'main:refs/heads/dev/remote-only')
        result = subprocess.run([sys.executable, verifier.__file__, '--repo', str(self.repo),
                                 '--capture-branches'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        captured = json.loads(result.stdout)
        self.assertEqual(captured['local']['dev/task'], self.head)
        self.assertEqual(captured['remote']['dev/remote-only'], self.head)
        self.assertNotIn('dev/remote-only', captured['local'])

    def test_removed_directory_must_be_gone_and_unregistered(self):
        self.remove_refs()
        wt = self.root / 'retained worktree'
        self.g('worktree', 'add', '--detach', str(wt), 'main')
        self.ledger['directories'] = [{'path': str(wt), 'head': self.head, 'outcome': 'removed'}]
        self.capture_runtime()
        self.assertIn('directory:' + str(wt) + ':directory-or-registration-remains', self.verify()['failures'])

    def test_retained_directory_checked_independently_of_branch(self):
        self.remove_refs()
        wt = self.root / 'retained worktree'
        self.g('worktree', 'add', '--detach', str(wt), 'main')
        self.ledger['directories'] = [{'path': str(wt), 'head': self.head, 'outcome': 'retained',
                                       'retention': 'blocked', 'reason': 'App archive unavailable'}]
        self.capture_runtime()
        self.assertEqual(self.verify()['closeout'], 'partial')

    def test_remote_query_failure_cannot_pass(self):
        self.remove_refs()
        self.g('remote', 'set-url', 'origin', str(self.root / 'missing-origin'))
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.verify()

    def test_target_branch_cannot_be_cleanup_candidate(self):
        self.ledger['candidates'][0]['branch'] = 'main'
        with self.assertRaisesRegex(ValueError, 'exclude'):
            self.verify()

    def test_cli_partial_is_nonzero(self):
        self.g('push', 'origin', '--delete', 'dev/task')
        self.ledger['candidates'][0]['local'].update(outcome='retained', retention='blocked',
                                                  reason='Git query unavailable', review=self.retention_review())
        ledger = self.root / 'ledger.json'
        ledger.write_text(json.dumps(self.ledger))
        result = subprocess.run([sys.executable, verifier.__file__, '--repo', str(self.repo), '--ledger', str(ledger)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(json.loads(result.stdout)['closeout'], 'partial')

    def test_invalid_top_level_json_returns_input_error(self):
        ledger = self.root / 'invalid.json'
        ledger.write_text('[]')
        result = subprocess.run([sys.executable, verifier.__file__, '--repo', str(self.repo), '--ledger', str(ledger)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['closeout'], 'blocked')

    def local_only(self):
        self.g('remote', 'remove', 'origin')
        self.ledger['base'].update(ref='main', location='local')
        self.ledger['candidates'][0]['remote']={'head':None,'outcome':'absent'}
        self.capture_branch_inventory()

    def test_local_only_removed_branch_passes(self):
        self.local_only(); self.g('branch','-d','dev/task')
        self.assertEqual(self.verify()['closeout'],'complete')

    def test_local_mode_cannot_hide_existing_remote(self):
        self.ledger['base'].update(ref='main',location='local')
        with self.assertRaisesRegex(ValueError,'remote'):
            self.verify()

    def test_local_mode_dirty_main_still_blocks(self):
        self.local_only(); self.g('branch','-d','dev/task')
        (self.repo/'dirty.txt').write_text('user work')
        self.assertIn('main-has-unaccounted-changes',self.verify()['failures'])

    def test_detached_directory_and_removed_branch_report_partial(self):
        self.local_only()
        wt=self.root/'kept';self.g('worktree','add',str(wt),'dev/task')
        self.g('switch','--detach',self.head,cwd=wt)
        self.g('branch','-d','dev/task')
        self.ledger['directories']=[{'path':str(wt),'head':self.head,'outcome':'retained','retention':'blocked','reason':'Incomplete app inventory'}]
        self.capture_runtime()
        self.assertEqual(self.verify()['closeout'],'partial')
        self.assertEqual(self.g('rev-parse','HEAD',cwd=wt),self.head)

    def resource(self, name='cache/result.bin', content=b'test artifact'):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def removed_cache(self):
        return {'kind': 'cache', 'action': 'removed', 'reason': 'Task-only generated cache',
                'source': 'isolated fixture task record', 'rebuild': 'Run the fixture generator'}

    def test_missing_runtime_review_is_partial_even_after_refs_are_removed(self):
        self.remove_refs()
        self.resource('.runtime/test-db/PG_VERSION', b'17')
        del self.ledger['runtime']
        result = self.verify()
        self.assertEqual(result['closeout'], 'partial')
        self.assertIn('runtime-resources-not-assessed', result['pending'])
        self.assertEqual(result['runtime']['status'], 'not-assessed')

    def test_existing_data_cannot_be_omitted(self):
        self.remove_refs()
        self.resource()
        self.capture_runtime()
        result = self.verify()
        self.assertEqual(result['closeout'], 'blocked')
        self.assertIn('runtime:' + str(self.repo) + ':ignored-data-not-fully-classified', result['failures'])

    def test_claimed_removal_checks_files_without_deleting_them(self):
        self.remove_refs()
        path = self.resource()
        self.capture_runtime({str(self.repo): {'cache/': self.removed_cache()}})
        result = self.verify()
        self.assertEqual(result['closeout'], 'blocked')
        self.assertTrue(any('declared-removed-data-still-exists' in item for item in result['failures']))
        self.assertEqual(path.read_bytes(), b'test artifact')

    def test_removed_cache_reports_before_and_after_usage(self):
        self.remove_refs()
        path = self.resource(content=b'x' * 8192)
        self.capture_runtime({str(self.repo): {'cache/': self.removed_cache()}})
        path.unlink()
        path.parent.rmdir()
        result = self.verify()
        self.assertEqual(result['closeout'], 'complete')
        self.assertEqual(result['runtime']['totals']['logical_bytes_decrease'], 8192)
        self.assertEqual(result['runtime']['totals']['logical_bytes_after'], 0)
        self.assertTrue(result['runtime']['filesystems'])
        self.assertTrue(self.repo.exists())

    def test_agreed_cache_retention_does_not_require_worktree_deletion(self):
        self.remove_refs()
        path = self.resource()
        keep = {'kind': 'cache', 'action': 'retained', 'retention': 'agreed',
                'reason': 'User keeps the preview', 'source': 'Fixture user instruction',
                'review': 'Review when the preview is closed'}
        self.capture_runtime({str(self.repo): {'cache/': keep}})
        self.assertEqual(self.verify()['closeout'], 'complete')
        self.assertTrue(path.exists())

    def test_unknown_data_cannot_be_marked_as_agreed_to_hide_a_blocker(self):
        self.remove_refs()
        self.resource()
        keep = {'kind': 'unknown', 'action': 'retained', 'retention': 'agreed',
                'reason': 'Ownership is unresolved', 'source': 'Fixture inspection', 'review': 'Identify owner'}
        self.capture_runtime({str(self.repo): {'cache/': keep}})
        self.assertEqual(self.verify()['closeout'], 'partial')

    def test_unknown_data_cannot_be_declared_disposable(self):
        self.remove_refs()
        path = self.resource()
        decision = self.removed_cache()
        decision['kind'] = 'unknown'
        self.capture_runtime({str(self.repo): {'cache/': decision}})
        path.unlink()
        path.parent.rmdir()
        self.assertEqual(self.verify()['closeout'], 'blocked')

    def test_new_ignored_data_after_inventory_must_be_classified(self):
        self.remove_refs()
        self.resource()
        keep = {'kind': 'cache', 'action': 'retained', 'retention': 'agreed',
                'reason': 'Preview', 'source': 'Fixture user instruction', 'review': 'Preview shutdown'}
        self.capture_runtime({str(self.repo): {'cache/': keep}})
        self.resource('.runtime/new-db/PG_VERSION', b'17')
        self.assertEqual(self.verify()['closeout'], 'blocked')

    def test_retention_requires_a_review_event(self):
        self.remove_refs()
        self.resource()
        keep = {'kind': 'test-data', 'action': 'retained', 'retention': 'agreed',
                'reason': 'Keep the test preview', 'source': 'Fixture user instruction'}
        self.capture_runtime({str(self.repo): {'cache/': keep}})
        self.assertTrue(any('next-review-not-recorded' in item for item in self.verify()['failures']))

    def archive(self):
        self.remove_refs()
        source = self.resource('.runtime/evidence.log', b'necessary evidence')
        backup = self.root / 'evidence.log'
        backup.write_bytes(source.read_bytes())
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        decision = {'kind': 'evidence', 'action': 'archived', 'reason': 'Keep required evidence outside the task',
                    'source': 'Fixture acceptance record', 'backup': str(backup), 'sha256': digest}
        self.capture_runtime({str(self.repo): {'.runtime/evidence.log': decision}})
        source.unlink()
        return backup

    def test_archived_material_is_reported_separately_from_free_space(self):
        backup = self.archive()
        result = self.verify()
        self.assertEqual(result['closeout'], 'complete')
        self.assertEqual(result['runtime']['totals']['archived_logical_bytes'], backup.stat().st_size)
        self.assertIn('not a guarantee', result['runtime']['measurement_note'])

    def test_bad_archive_cannot_pass(self):
        backup = self.archive()
        backup.write_bytes(b'corrupt')
        self.assertTrue(any('archive-content-mismatch' in item for item in self.verify()['failures']))

    def test_before_inventory_tampering_fails_closed(self):
        self.remove_refs()
        Path(self.ledger['runtime']['before_inventory']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.verify()

    def test_summary_is_not_a_before_inventory(self):
        self.remove_refs()
        runtime = self.ledger['runtime']
        path = Path(runtime['before_inventory'])
        path.write_text(json.dumps(verifier.git_state.summary(json.loads(path.read_text()))))
        runtime['before_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.verify()

    def test_live_service_cannot_be_reported_as_stopped(self):
        self.remove_refs()
        self.ledger['runtime']['services']['items'] = [
            {'name': 'fixture', 'outcome': 'stopped', 'pid': os.getpid(),
             'reason': 'Claimed stopped', 'source': 'Fixture process query'}]
        self.assertTrue(any('declared-stopped-process-still-exists' in item for item in self.verify()['failures']))

    def test_incomplete_or_stale_service_query_is_partial(self):
        self.remove_refs()
        services = self.ledger['runtime']['services']
        services.update(complete=False, observed_at_unix=time.time() - 300)
        result = self.verify()
        self.assertEqual(result['closeout'], 'partial')
        self.assertIn('runtime-service-query-incomplete', result['pending'])
        self.assertIn('runtime-service-query-not-fresh', result['pending'])

    def test_confirmed_stopped_service_passes_read_only(self):
        self.remove_refs()
        self.ledger['runtime']['services']['items'] = [
            {'name': 'fixture', 'outcome': 'stopped', 'pid': 123456,
             'reason': 'Own test process exited', 'source': 'Fixture process query'}]
        with patch.object(verifier.runtime_resources, 'alive', return_value=False):
            self.assertEqual(self.verify()['closeout'], 'complete')

    def test_new_data_during_service_check_cannot_escape_final_review(self):
        self.remove_refs()
        def changed(_services):
            self.resource('.runtime/late-file')
            return [], []
        with patch.object(verifier.runtime_resources, 'services_check', side_effect=changed):
            result = self.verify()
        self.assertTrue(any('changed-during-verification' in item for item in result['failures']))

    def retained_online_log(self):
        self.remove_refs()
        path = self.resource('.runtime/online.log', b'original log\n')
        raw = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart=', '-o', 'command=']).strip()
        source = self.root / 'owner-observation.txt'
        source.write_bytes(raw + b'\nFixture owns exact retained online log.\n')
        rule = {'kind': 'runtime', 'action': 'retained', 'retention': 'required',
                'reason': 'Existing daily fixture service', 'source': 'Fixture ownership record',
                'dynamic': {'path': str(path), 'role': 'append-log', 'pid': os.getpid(),
                            'owner_signature': hashlib.sha256(raw).hexdigest(),
                            'observed_at_unix': time.time(),
                            'source': {'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}}}
        self.capture_runtime({str(self.repo): {'.runtime/online.log': rule}})
        return path, source, rule

    def test_retained_live_log_update_is_not_a_shutdown_requirement(self):
        path, _, _ = self.retained_online_log()
        with path.open('ab') as log:
            log.write(b'before verification\n')
        def append(_services):
            with path.open('ab') as log:
                log.write(b'during verification\n')
            return [], []
        with patch.object(verifier.runtime_resources, 'services_check', side_effect=append):
            result = self.verify()
        self.assertEqual(result['closeout'], 'complete', result['failures'])
        self.assertTrue(result['runtime']['dynamic_observations'])

    def test_live_log_does_not_hide_other_changes(self):
        path, _, _ = self.retained_online_log()
        def change(_services):
            with path.open('ab') as log:
                log.write(b'normal output\n')
            self.resource('.runtime/undeclared-file')
            return [], []
        with patch.object(verifier.runtime_resources, 'services_check', side_effect=change):
            result = self.verify()
        self.assertTrue(any('changed-during-verification' in item for item in result['failures']))

    def test_live_log_replacement_during_verification_is_rejected(self):
        path, _, _ = self.retained_online_log()
        def replace(_services):
            replacement = path.with_suffix('.new')
            replacement.write_bytes(b'replacement has more bytes than original\n')
            replacement.replace(path)
            return [], []
        with patch.object(verifier.runtime_resources, 'services_check', side_effect=replace):
            result = self.verify()
        self.assertTrue(any('changed-during-verification' in item for item in result['failures']))

    def test_live_owner_source_is_rechecked_even_without_file_updates(self):
        _, source, _ = self.retained_online_log()
        def change_source(_services):
            source.write_text('changed owner evidence')
            return [], []
        with patch.object(verifier.runtime_resources, 'services_check', side_effect=change_source):
            result = self.verify()
        self.assertTrue(any('changed-during-verification' in item for item in result['failures']))

    def test_retained_live_json_cache_can_update_atomically(self):
        log, source, rule = self.retained_online_log()
        path = log.with_name('catalog.json')
        log.unlink()
        path.write_text('{"fixture":1}')
        source.write_text('Fixture owns exact rebuildable JSON cache path.')
        rule.update(kind='cache', retention='agreed', rebuild='Recreate from public fixture catalog',
                    review='When fixture service ends')
        rule['dynamic'].update(path=str(path), role='replace-cache',
                               source={'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
        self.capture_runtime({str(self.repo): {'.runtime/catalog.json': rule}})
        def replace(_services):
            temporary = path.with_suffix('.new')
            temporary.write_text('{"fixture":2,"generated":true}')
            temporary.replace(path)
            return [], []
        with patch.object(verifier.runtime_resources, 'services_check', side_effect=replace):
            result = self.verify()
        self.assertEqual(result['closeout'], 'complete', result['failures'])
        self.assertTrue(result['runtime']['dynamic_observations'])

    def test_equivalent_absolute_path_aliases_are_the_same_runtime_root(self):
        self.remove_refs()
        alias = self.root / 'repo-alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.ledger['main_path'] = str(alias)
        self.ledger['runtime']['classifications'] = {str(alias): {}}
        self.assertEqual(self.verify()['closeout'], 'complete')

    def test_duplicate_runtime_roots_through_aliases_are_rejected(self):
        self.remove_refs()
        alias = self.root / 'repo-alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.ledger['runtime']['classifications'][str(alias)] = {}
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.verify()

    def kept_material(self, kind):
        self.remove_refs()
        data = self.resource('.runtime/material.bin')
        decision = {'kind': kind, 'action': 'retained', 'retention': 'required',
                    'reason': 'Required original material', 'source': 'Fixture preservation record'}
        self.capture_runtime({str(self.repo): {'.runtime/material.bin': decision}})
        return data

    def dangling_material_is_blocked(self, kind):
        data = self.kept_material(kind)
        data.unlink()
        data.symlink_to(self.root / 'missing-original')
        result = self.verify()
        self.assertEqual(result['closeout'], 'blocked')
        self.assertTrue(any('not-ordinary-files' in item for item in result['failures']))

    def test_required_evidence_replaced_by_a_dangling_link_cannot_pass(self):
        self.dangling_material_is_blocked('evidence')

    def test_business_data_replaced_by_a_dangling_link_cannot_pass(self):
        self.dangling_material_is_blocked('business-data')

    def test_cleared_necessary_material_requires_review(self):
        data = self.kept_material('evidence')
        data.write_bytes(b'')
        result = self.verify()
        self.assertEqual(result['closeout'], 'partial')
        self.assertTrue(any('changed-needs-review' in item for item in result['pending']))

    def test_unchanged_necessary_material_passes(self):
        self.kept_material('evidence')
        self.assertEqual(self.verify()['closeout'], 'complete')

    def test_test_database_cannot_lose_files_while_claimed_retained(self):
        self.remove_refs()
        data = self.resource('.runtime/database/records')
        self.resource('.runtime/database/catalog')
        decision = {'kind': 'test-data', 'action': 'retained', 'retention': 'agreed',
                    'reason': 'User keeps test preview', 'source': 'Fixture user instruction',
                    'review': 'Preview shutdown'}
        self.capture_runtime({str(self.repo): {'.runtime/database/': decision}})
        data.unlink()
        result = self.verify()
        self.assertEqual(result['closeout'], 'blocked')
        self.assertTrue(any('missing-files' in item for item in result['failures']))

    def superseded_material(self, kind='evidence'):
        self.remove_refs()
        old = self.resource('.runtime/old-report/run.log')
        replacement = self.root / 'final-report.json'
        replacement.write_text('{"commit":"fixture","evidence":"retained originals"}')
        decision = {'kind': kind, 'action': 'superseded', 'reason': 'Final report replaces old run',
                    'source': 'Fixture explicit coverage review', 'coverage': 'Accurate result and necessary originals retained',
                    'replacement': str(replacement), 'sha256': hashlib.sha256(replacement.read_bytes()).hexdigest()}
        self.capture_runtime({str(self.repo): {'.runtime/old-report/': decision}})
        return old, replacement

    def test_superseded_old_evidence_checks_replacement_and_actual_removal(self):
        old, replacement = self.superseded_material()
        self.assertEqual(self.verify()['closeout'], 'blocked')
        old.unlink()
        old.parent.rmdir()
        self.assertEqual(self.verify()['closeout'], 'complete')
        replacement.write_text('wrong replacement')
        self.assertEqual(self.verify()['closeout'], 'blocked')

    def test_business_data_cannot_be_superseded_by_a_report(self):
        old, _ = self.superseded_material('business-data')
        old.unlink()
        old.parent.rmdir()
        result = self.verify()
        self.assertEqual(result['closeout'], 'blocked')
        self.assertTrue(any('supersession-not-explained' in f for f in result['failures']))

    def test_supersession_requires_explicit_coverage(self):
        old, _ = self.superseded_material()
        del self.ledger['runtime']['classifications'][str(self.repo)]['.runtime/old-report/']['coverage']
        old.unlink()
        old.parent.rmdir()
        self.assertEqual(self.verify()['closeout'], 'blocked')



if __name__ == '__main__':
    unittest.main()
