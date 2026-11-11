#!/usr/bin/env python3
"""Stopping evidence cannot substitute for unfinished work or grant deletion."""
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import closeout_progress as progress


class CloseoutProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shoukou-progress-', dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.item = {'object': 'local:dev/paused', 'version': 'a' * 40,
                     'next_action': 'Investigate this task'}
        self.record = self.root / 'original.txt'
        self.record.write_text('Fixture user paused this task and required preserving all work.')
        self.stop = {'version': self.item['version'], 'kind': 'user-pause',
                     'attempt': 'Read original task record', 'result': 'Task is paused',
                     'release_condition': 'User resumes this task',
                     'evidence': {'path': str(self.record),
                                  'sha256': hashlib.sha256(self.record.read_bytes()).hexdigest()}}

    def tearDown(self):
        self.tmp.cleanup()

    def assess(self, items=None):
        return progress.assess(items or [self.item], {self.item['object']: self.stop})

    def test_ordinary_todo_does_not_become_a_stopping_condition(self):
        self.assertEqual(progress.assess([self.item])['state'], 'continue')

    def test_template_text_without_original_record_is_unfinished(self):
        self.stop['evidence'] = 'Still need complete investigation'
        self.assertEqual(self.assess()['state'], 'continue')

    def test_valid_pause_protects_its_object_without_changing_evidence(self):
        before = self.record.read_bytes()
        result = self.assess()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['unfinished'], [])
        self.assertEqual(self.record.read_bytes(), before)

    def test_pause_does_not_excuse_another_unfinished_object(self):
        other = dict(self.item, object='runtime-resources-not-assessed')
        result = self.assess([self.item, other])
        self.assertEqual(result['state'], 'continue')
        self.assertEqual([x['object'] for x in result['unfinished']], [other['object']])

    def test_original_evidence_must_match_its_recorded_digest(self):
        self.record.write_text('User resumed the task')
        self.assertEqual(self.assess()['state'], 'continue')

    def test_stop_for_an_old_version_is_unfinished(self):
        self.stop['version'] = 'b' * 40
        self.assertEqual(self.assess()['state'], 'continue')

    def test_symlink_is_not_original_evidence(self):
        alias = self.root / 'alias'
        alias.symlink_to(self.record)
        self.stop['evidence']['path'] = str(alias)
        self.assertEqual(self.assess()['state'], 'continue')

    def test_not_yet_investigated_is_not_a_stop_kind(self):
        self.stop['kind'] = 'not-yet-investigated'
        self.assertEqual(self.assess()['state'], 'continue')

    def test_stale_stop_for_another_object_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'current unfinished objects'):
            progress.assess([self.item], {'local:dev/other': self.stop})

    def test_no_remaining_work_is_complete(self):
        self.assertEqual(progress.assess([])['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
