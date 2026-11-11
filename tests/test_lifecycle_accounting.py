"""Optional lifecycle integration fails closed and binds exact file versions.

The task-guard protocol is supplied explicitly so these tests also run in a
clean checkout where that separately developed component is not installed.
Real task-guard integration is checked separately in the local development tree.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import storage_audit as audit
import verify_closeout as verifier


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def signature(path):
    info = path.lstat()
    return dict(type='file', bytes=info.st_size, mode='100644',
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


class LifecycleAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.file = self.repo / 'design.html'
        self.file.write_text('owner-approved version')
        self.original = dict(working=dict(type='file', sha256='original'))
        self.current = dict(working=signature(self.file))
        self.state = dict(requirements=['commit', 'local-main'],
                          baseline=dict(worktrees={str(self.repo): dict(changes={'design.html': self.original})}))
        self.key = 'protected-change:' + str(self.repo) + ':design.html'
        self.decision = dict(kind='owner-update', version=digest([
            'selected', self.key, dict(before=self.original, after=self.current)]))
        self.result = dict(state='complete', original_main=str(self.repo),
                           accepted_decisions=[dict(object=self.key, decision=self.decision)])
        guard = types.ModuleType('task_guard')
        guard.store = lambda *args: contextlib.nullcontext((self.repo, self.repo / 'state.json'))
        guard.regular_json = lambda *args: self.state
        guard.check_task = Mock(side_effect=lambda *args, **kw: self.result)
        guard.config_of = lambda *args: ({}, 'config-digest')
        guard.source_snapshot = Mock(side_effect=lambda *args: dict(changes={'design.html': self.current}))
        guard.digest = digest
        guard.signature = signature
        self.guard = guard
        self.modules = patch.dict(sys.modules, {'task_guard': guard})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.plan = {'lifecycle_task': 'selected'}

    def test_missing_component_or_record_keeps_protection(self):
        for plan in [{}, self.plan]:
            with patch.dict(sys.modules, {'task_guard': None}):
                self.assertFalse(verifier.protected_main_accounted(self.repo, self.repo, plan))
                self.assertEqual(audit.lifecycle_owner_updates(self.repo, plan), {})
        for error in [OSError('unreadable'), ValueError('invalid state'), KeyError('missing')]:
            self.guard.check_task.side_effect = error
            self.assertFalse(verifier.protected_main_accounted(self.repo, self.repo, self.plan))
            self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})

    def test_incomplete_lifecycle_or_missing_requirements_cannot_waive_changes(self):
        for state in ['continue', 'blocked']:
            self.result['state'] = state
            self.assertFalse(verifier.protected_main_accounted(self.repo, self.repo, self.plan))
            self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})
        self.result['state'] = 'complete'
        self.state['requirements'] = ['cleanup']
        self.assertFalse(verifier.protected_main_accounted(self.repo, self.repo, self.plan))
        self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})

    def test_main_must_be_the_original_registered_main(self):
        self.assertTrue(verifier.protected_main_accounted(self.repo, self.repo, self.plan))
        self.assertFalse(verifier.protected_main_accounted(self.repo, self.repo / 'wrong', self.plan))

    def test_only_current_owner_attribution_is_accepted(self):
        self.assertEqual(set(audit.lifecycle_owner_updates(self.repo, self.plan)), {str(self.file)})
        self.decision['kind'] = 'other-task'
        self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})
        self.decision['kind'] = 'owner-update'
        self.decision['version'] = 'stale'
        self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})

    def test_file_change_between_snapshot_and_measurement_is_rejected(self):
        self.file.write_text('unreviewed newer version')
        self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})

    def test_file_change_while_hashing_is_rejected(self):
        before = signature(self.file)
        def racing_signature(path):
            path.write_text('changed while hashing')
            return before
        self.guard.signature = racing_signature
        self.assertEqual(audit.lifecycle_owner_updates(self.repo, self.plan), {})


if __name__ == '__main__':
    unittest.main()
