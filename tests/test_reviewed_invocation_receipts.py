"""An explicit receipt review preserves failed invocations, not failed tests."""
import copy
import json
from pathlib import Path
import sys
import unittest

import test_task_guard as fixtures

guard = fixtures.guard


class ReviewedInvocationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskGuardTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.begin(['tests', 'cleanup'])

    def run_command(self, code):
        return guard.run_task(self.f.repo, 'task', [sys.executable, '-c', code])['run']

    def reviews(self, result, proof):
        items = []
        for finding in result['unfinished']:
            kind = 'test-record' if finding['object'] == 'tests-selection' else 'reviewed-invocation'
            items.append(dict(object=finding['object'], version=finding['version'], kind=kind,
                              reason='Explicit review of corrected invocation and exact synthetic test receipt',
                              evidence=proof['receipt']))
        path = self.f.base / 'review.json'
        path.write_text(json.dumps(items))
        return path

    def test_explicit_review_records_original_failure_without_rewriting_it(self):
        old = self.run_command('raise SystemExit(2)')
        good = self.run_command('print("corrected invocation")')
        original = copy.deepcopy(self.f.state()['runs'][0])
        pending = self.f.check()
        self.assertNotEqual(pending['state'], 'complete')
        reviewed = self.f.check(decision_paths=[self.reviews(pending, good)])
        self.assertEqual(reviewed['state'], 'complete', reviewed)
        self.assertEqual(self.f.state()['runs'][0], original)
        self.assertEqual(original['exit_code'], 2)
        self.assertTrue(Path(old['receipt']['path']).exists())

    def test_named_test_failure_cannot_be_reviewed_as_an_invocation(self):
        guard.run_task(self.f.repo, 'task', [], 'other')
        good = self.run_command('print("unrelated success")')
        pending = self.f.check()
        reviewed = self.f.check(decision_paths=[self.reviews(pending, good)])
        self.assertNotEqual(reviewed['state'], 'complete')
        self.assertTrue(any(x['object'].startswith('run:') for x in reviewed['unfinished']))

    def test_review_does_not_survive_a_later_candidate_change(self):
        self.run_command('raise SystemExit(2)')
        good = self.run_command('print("corrected invocation")')
        self.assertEqual(self.f.check(decision_paths=[self.reviews(self.f.check(), good)])['state'], 'complete')
        (self.f.repo / 'source.txt').write_text('different source')
        self.assertNotEqual(self.f.check()['state'], 'complete')

    def test_stale_candidate_receipt_does_not_clear_new_failure(self):
        old_good = self.run_command('print("old success")')
        (self.f.repo / 'source.txt').write_text('new source')
        self.run_command('raise SystemExit(2)')
        pending = self.f.check()
        with self.assertRaisesRegex(ValueError, 'current candidate'):
            self.f.check(decision_paths=[self.reviews(pending, old_good)])

    def test_valid_json_outside_this_task_is_not_a_run_receipt(self):
        self.run_command('raise SystemExit(2)')
        good = self.run_command('print("ok")')
        path = self.reviews(self.f.check(), good)
        data = json.loads(path.read_text())
        unrelated = self.f.base / 'unrelated.json'
        unrelated.write_text('{"exit_code":0}')
        for item in data:
            item['evidence'] = {'path': str(unrelated), 'sha256': guard.signature(unrelated)['sha256']}
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'current candidate'):
            self.f.check(decision_paths=[path])
