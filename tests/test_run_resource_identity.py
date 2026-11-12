"""A terminal owned run must not become live when its numeric PGID is reused."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import sys

import test_task_guard as fixtures
guard = fixtures.guard


class RunResourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TaskGuardTests('runTest'); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.f.begin(['cleanup'])

    def recorded(self, code='print("owned run finished")'):
        return guard.run_task(self.f.repo, 'task', [sys.executable, '-c', code])['run']

    def test_terminal_receipt_closes_exact_run_despite_reused_group(self):
        run = self.recorded(); before = Path(run['receipt']['path']).read_bytes()
        with patch.object(guard, 'group_active', return_value=True):
            self.assertIs(guard.run_group_active(run), False)
            self.assertEqual(self.f.check()['state'], 'complete')
        self.assertEqual(Path(run['receipt']['path']).read_bytes(), before)
        self.core_evidence = {'terminal_receipt_unchanged': True, 'work_absent': not (Path(run['root'])/'work').exists()}

    def test_closed_resource_does_not_turn_nonzero_execution_green(self):
        run = self.recorded('raise SystemExit(9)')
        with patch.object(guard, 'group_active', return_value=True):
            self.assertIs(guard.run_group_active(run), False)
            result = self.f.check()
            self.assertNotEqual(result['state'], 'complete')
            self.assertTrue(any(x['object'].startswith('run:') for x in result['unfinished']))
            self.assertFalse(any(x['object'].startswith('run-resource:') for x in result['unfinished']))

    def test_published_invocation_review_version_remains_accepted(self):
        failed = self.recorded('raise SystemExit(9)')
        proof = self.recorded()
        key = 'run:' + failed['command_key']
        pending = self.f.check()
        finding = next(x for x in pending['unfinished'] if x['object'] == key)
        # The already-published 402/36dc projection is a compatibility contract.
        facts = {'run_id': failed['id'], 'candidate': pending['candidate'],
                 'status': failed['status'], 'exit_code': failed['exit_code'],
                 'violations': failed['violations'], 'group_active_now': False}
        legacy_version = guard.digest(['task', key, facts])
        self.assertEqual(finding['version'], legacy_version)
        decision = dict(object=key, version=legacy_version, kind='reviewed-invocation',
                        reason='Retain the exact published invocation review projection', evidence=proof['receipt'])
        path = self.f.base/'published-review.json'; path.write_text(json.dumps(decision))
        before = copy.deepcopy(self.f.state()['runs'])
        self.assertEqual(self.f.check(decision_paths=[path])['state'], 'complete')
        self.assertEqual(self.f.state()['runs'], before)

    def test_missing_drifted_receipt_unknown_group_or_reappeared_work_is_not_terminal(self):
        run = self.recorded()
        with patch.object(guard, 'group_active', return_value=True):
            for field, value in [('receipt', None), ('root_identity', [0, 0]), ('exit_code', False), ('pgid', 0), ('pgid', None)]:
                other = copy.deepcopy(run); other[field] = value
                self.assertIsNot(guard.run_group_active(other), False)
            root = Path(run['root']); work = root/'work'
            work.mkdir()
            self.assertIs(guard.run_group_active(run), True)
            work.rmdir(); work.symlink_to(root/'missing-work')
            self.assertIs(guard.run_group_active(run), True)
            result = self.f.check(); self.assertNotEqual(result['state'], 'complete')
            work.unlink()
            receipt = Path(run['receipt']['path']); original = receipt.read_bytes()
            try:
                receipt.write_text('{}')
                self.assertIs(guard.run_group_active(run), True)
            finally:
                receipt.write_bytes(original)

    def test_active_or_unknown_descendants_cannot_use_terminal_receipt(self):
        run = self.recorded(); path = Path(run['receipt']['path']); original = path.read_bytes()
        for active in [True, None]:
            other = copy.deepcopy(run); other['active_descendants'] = active
            sealed = json.loads(original); sealed['active_descendants'] = active
            path.write_text(json.dumps(sealed))
            other['receipt']['sha256'] = guard.signature(path)['sha256']
            with patch.object(guard, 'group_active', return_value=True):
                self.assertIs(guard.run_group_active(other), True)
        path.write_bytes(original)


class CapturedRunResourceTest(unittest.TestCase):
    evidence = None
    repo = None

    def test_actual_completed_047_resource_is_not_the_reused_system_group(self):
        if self.evidence is None: self.skipTest('Actual resource evidence was not supplied')
        folder = guard.no_links(Path(self.evidence))
        metadata = json.loads((folder/'SOURCE.json').read_text())
        for name, sha in metadata['files'].items():
            self.assertEqual(guard.signature(folder/name)['sha256'], sha)
        task_file = guard.common_of(self.repo)/'codex-workflow'/(metadata['task']+'.json')
        self.assertEqual(task_file, Path(metadata['task_file']))
        state = json.loads(task_file.read_text())
        entry = next(x for x in state['runs'] if x.get('id')==metadata['run_id'])
        captured = json.loads((folder/'original-record.json').read_text())
        self.assertEqual(guard.digest(entry), guard.digest(captured))
        original = Path(entry['receipt']['path']).read_bytes()
        self.assertEqual(original, (folder/'original-result.json').read_bytes())
        before = guard.source_snapshot(self.repo, guard.config_of(self.repo)[0])
        raw_group_active = guard.group_active(entry['pgid'])
        self.assertIs(guard.run_group_active(entry), False)
        self.assertEqual(guard.source_snapshot(self.repo, guard.config_of(self.repo)[0]), before)
        self.assertEqual(Path(entry['receipt']['path']).read_bytes(), original)
        self.core_evidence = {'run_id': entry['id'], 'original_exit_code': entry['exit_code'],
            'raw_group_number_active': raw_group_active, 'owned_run_active': False,
            'original_receipt_unchanged': True, 'original_record_unchanged': True,
            'work_absent': not (Path(entry['root'])/'work').exists(), 'project_source_unchanged': True,
            'scope': 'Read-only observation; no signal or deletion of current processes'}


if __name__ == '__main__': unittest.main()
