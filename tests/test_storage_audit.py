#!/usr/bin/env python3
"""Full-space checks against isolated repositories; no real project cleanup."""
import os
import json
import hashlib
import time
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import storage_audit as audit


class StorageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shoukou-space-', dir=Path.cwd())
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'project'
        self.git('init', '-b', 'main', str(self.repo), cwd=self.root)
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'source.txt').write_text('maintained source\n')
        (self.repo / '.gitignore').write_text('cache/\n')
        self.git('add', '.')
        self.git('commit', '-m', 'base')
        self.before_path = self.root / 'before.json.gz'

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        r = subprocess.run(['git', '-C', str(cwd or self.repo), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def file(self, name, data=b'fixture data', root=None):
        path = (root or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def keep(self, kind='source'):
        return {'kind': kind, 'action': 'retained', 'retention': 'required',
                'reason': 'Required fixture material', 'source': 'Fixture user/policy record',
                'need': 'Supports final fixture outcome', 'review': 'When replaced by verified final evidence'}

    def removal(self, kind='test-data'):
        return {'kind': kind, 'action': 'removed', 'reason': 'Isolated batch ended',
                'source': 'Fixture ownership and dependency query', 'disposal_basis': 'Rebuild from fixed fixture seed'}

    def start(self, overrides=None, extras=()):
        before = audit.capture(self.repo, extras)
        audit.save(self.before_path, before)
        rules = {}
        for state in before['roots']:
            # Explicit top-level groups; no broad whole-root rule.
            groups = {name.split('/')[0] for name in state['entries']}
            rules[state['path']] = {g + ('/' if (Path(state['path']) / g).is_dir() else ''): self.keep('git-metadata' if g == '.git' else 'source')
                                    for g in groups}
        for root, changes in (overrides or {}).items():
            rules[str(root)].update(changes)
        self.review = {'before_inventory': str(self.before_path), 'before_sha256': audit.digest(self.before_path),
                       'classifications': rules}
        return before

    def verify(self):
        return audit.verify(self.repo, self.review)

    def test_empty_root_classification_cli_reports_continue_with_exact_roots(self):
        self.start()
        self.review['classifications'] = {}
        path = self.root / 'review.json'
        path.write_text(json.dumps(self.review))
        result = subprocess.run([sys.executable, audit.__file__, 'verify', '--repo', str(self.repo),
                                 '--review', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data['status'], 'incomplete')
        self.assertEqual(data['execution']['state'], 'continue')
        self.assertIn('storage-root:' + str(self.repo),
                      [x['object'] for x in data['execution']['unfinished']])
        self.assertTrue((self.repo / 'source.txt').is_file())

    def test_empty_group_classification_cli_reports_unfinished_entries(self):
        self.start()
        self.review['classifications'][str(self.repo)] = {}
        path = self.root / 'review.json'
        path.write_text(json.dumps(self.review))
        result = subprocess.run([sys.executable, audit.__file__, 'verify', '--repo', str(self.repo),
                                 '--review', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stdout)
        data = json.loads(result.stdout)
        self.assertIn('storage-entry:' + str(self.repo / 'source.txt'),
                      [x['object'] for x in data['execution']['unfinished']])

    def test_missing_classification_field_lists_work_instead_of_external_blocker(self):
        self.start()
        del self.review['classifications']
        with self.assertRaises(audit.progress.IncompleteReview) as error:
            self.verify()
        self.assertIn('storage-root:' + str(self.repo), [x['object'] for x in error.exception.items])

    def test_unknown_material_is_a_todo_without_original_stop_evidence(self):
        self.file('old-cache/content')
        self.start({self.repo: {'old-cache/': self.keep('unknown')}})
        result = self.verify()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['execution']['state'], 'continue')
        self.assertTrue((self.repo / 'old-cache/content').exists())

    def test_counts_nonignored_nested_repository_and_common_git(self):
        nested = self.repo / 'acceptance-platform'
        self.git('init', str(nested), cwd=self.root)
        self.file('large-copy.bin', b'x' * 16384, root=nested)
        before = self.start()
        entries = before['roots'][0]['entries']
        self.assertIn('acceptance-platform/large-copy.bin', entries)
        self.assertIn('acceptance-platform/.git/HEAD', entries)
        self.assertIn('.git/HEAD', entries)
        self.assertGreater(before['totals']['logical_bytes'], 16384)
        self.assertEqual(self.verify()['status'], 'complete')

    def test_missing_ignored_only_scope_cannot_be_used_as_full_capture(self):
        audit_data = {'schema_version': 1, 'kind': 'ignored-only'}
        audit.save(self.before_path, audit_data)
        with self.assertRaisesRegex(ValueError, 'full storage'):
            audit.verify(self.repo, {'before_inventory': str(self.before_path),
                                    'before_sha256': audit.digest(self.before_path)})

    def test_registered_worktree_and_overlapping_extra_count_once(self):
        wt = self.root / 'task'
        self.git('worktree', 'add', '-b', 'dev/task', str(wt))
        self.file('cache/payload', b'x' * 9000, root=wt)
        before = self.start(extras=[wt, wt / 'cache'])
        self.assertEqual(len(before['roots']), 2)
        self.assertEqual(sum('cache/payload' in r['entries'] for r in before['roots']), 1)
        self.assertEqual(self.verify()['status'], 'complete')

    def test_unclassified_new_file_and_overlapping_groups_rejected(self):
        self.start()
        self.file('late-snapshot.bin')
        with self.assertRaises(audit.progress.IncompleteReview) as error:
            self.verify()
        self.assertIn('storage-entry:' + str(self.repo / 'late-snapshot.bin'),
                      [x['object'] for x in error.exception.items])
        (self.repo / 'late-snapshot.bin').unlink()
        self.review['classifications'][str(self.repo)]['.git/HEAD'] = self.keep()
        with self.assertRaisesRegex(ValueError, 'omit, overlap'):
            self.verify()

    def test_root_catchall_is_rejected(self):
        self.start()
        self.review['classifications'][str(self.repo)] = {'./': self.keep()}
        with self.assertRaisesRegex(ValueError, 'whole-root'):
            self.verify()

    def test_claimed_removal_is_read_only_and_checks_empty_directory(self):
        self.file('test-db/records')
        self.start({self.repo: {'test-db/': self.removal()}})
        self.assertEqual(self.verify()['status'], 'blocked')
        self.assertTrue((self.repo / 'test-db/records').exists())
        (self.repo / 'test-db/records').unlink()
        self.assertEqual(self.verify()['status'], 'blocked')
        (self.repo / 'test-db').rmdir()
        self.assertEqual(self.verify()['status'], 'complete')

    def test_removal_requires_basis_and_protects_business_kind(self):
        p = self.file('database')
        self.start({self.repo: {'database': self.removal('business-data')}})
        p.unlink()
        result = self.verify()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('protected-kind' in f for f in result['failures']))

    def test_external_backups_and_new_evidence_count_toward_totals(self):
        old = self.file('old-copy/file', b'x' * 8192)
        external = self.root / 'external-records'
        before = self.start({self.repo: {'old-copy/': self.removal('copy')}}, extras=[external])
        self.assertFalse(before['roots'][1]['exists'])
        shutil.rmtree(old.parent)
        self.file('report.bin', b'y' * 8192, root=external)
        self.review['classifications'][str(external)] = {'report.bin': self.keep('evidence')}
        result = self.verify()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['logical_bytes_decrease'], 0)

    def test_unknown_and_unexplained_history_retention_not_complete(self):
        self.file('old-audit/log')
        decision = self.keep('audit')
        del decision['need']
        self.start({self.repo: {'old-audit/': decision}})
        self.assertEqual(self.verify()['status'], 'blocked')
        decision.update(kind='unknown', need='Investigate ownership', retention='required')
        self.assertEqual(self.verify()['status'], 'partial')

    def test_superseded_report_requires_actual_counted_replacement(self):
        old = self.file('old-reports/run.log')
        final = self.file('final.json', b'{"commit":"fixture","evidence":["unique.png"]}')
        decision = {'kind': 'evidence', 'action': 'superseded', 'reason': 'Final report covers the batch',
                    'source': 'Fixture coverage review', 'coverage': 'Accurate result and original evidence index',
                    'replacement': str(final), 'sha256': audit.digest(final)}
        self.start({self.repo: {'old-reports/': decision, 'final.json': self.keep('evidence')}})
        self.assertEqual(self.verify()['status'], 'blocked')
        shutil.rmtree(old.parent)
        self.assertEqual(self.verify()['status'], 'complete')
        final.write_bytes(b'wrong report')
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_superseded_report_cannot_reference_uncounted_external_file(self):
        old = self.file('old.log')
        final = self.file('external.json', root=self.root)
        decision = {'kind': 'evidence', 'action': 'superseded', 'reason': 'Replaced',
                    'source': 'Fixture', 'coverage': 'Fixture coverage',
                    'replacement': str(final), 'sha256': audit.digest(final)}
        self.start({self.repo: {'old.log': decision}})
        old.unlink()
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_runtime_database_missing_or_changed_requires_review(self):
        data = self.file('daily-db/data')
        self.start({self.repo: {'daily-db/': self.keep('runtime')}})
        data.write_bytes(b'changed live database')
        self.assertEqual(self.verify()['status'], 'partial')
        data.unlink()
        data.symlink_to(self.root / 'missing-data')
        self.assertEqual(self.verify()['status'], 'blocked')

    def online_rule(self, path, role='append-log'):
        raw = subprocess.check_output(['ps', '-p', str(os.getpid()), '-o', 'lstart=', '-o', 'command=']).strip()
        evidence = self.root / 'owner-observation.txt'
        evidence.write_bytes(raw + b'\nFixture process owns the exact retained log/cache path.\n')
        rule = self.keep('runtime' if role == 'append-log' else 'cache')
        rule['dynamic'] = {'path': str(path), 'role': role, 'pid': os.getpid(),
                           'owner_signature': hashlib.sha256(raw).hexdigest(),
                           'observed_at_unix': time.time(),
                           'source': {'path': str(evidence), 'sha256': audit.digest(evidence)}}
        if role == 'replace-cache':rule['rebuild'] = 'Recreate disposable fixture cache from fixed public metadata'
        return rule

    def test_owned_online_log_append_is_retained_without_requiring_shutdown(self):
        path = self.file('online/service.log', b'original log\n')
        self.start({self.repo: {'online/': self.keep('runtime'),
                               'online/service.log': self.online_rule(path)}})
        del self.review['classifications'][str(self.repo)]['online/']
        self.review['classifications'][str(self.repo)]['online'] = self.keep('runtime')
        with path.open('ab') as log:log.write(b'normal service output\n')
        result = self.verify()
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['dynamic_observations'])
        self.assertEqual(path.read_bytes(), b'original log\nnormal service output\n')

    def test_owned_cache_atomic_update_during_recheck_is_observed(self):
        path = self.file('cache/catalog.json', b'{"fixture":1}')
        self.start({self.repo: {'cache/': self.keep('cache'),
                               'cache/catalog.json': self.online_rule(path, 'replace-cache')}})
        del self.review['classifications'][str(self.repo)]['cache/']
        self.review['classifications'][str(self.repo)]['cache'] = self.keep('cache')
        real_measure = audit.measure
        observations = 0
        def update(root):
            nonlocal observations
            if root == self.repo:
                observations += 1
                if observations == 2:
                    temporary = path.with_suffix('.new')
                    temporary.write_bytes(b'{"fixture":2,"generated":true}')
                    temporary.replace(path)
            return real_measure(root)
        with patch.object(audit, 'measure', side_effect=update):result = self.verify()
        self.assertEqual(result['status'], 'complete')

    def test_dynamic_log_cannot_disappear_or_become_a_symlink(self):
        path = self.file('online.log', b'original')
        self.start({self.repo: {'online.log': self.online_rule(path)}})
        path.unlink()
        self.assertEqual(self.verify()['status'], 'blocked')
        path.symlink_to(self.root / 'other')
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_dynamic_log_truncation_remains_blocked(self):
        path = self.file('online.log', b'original log content')
        self.start({self.repo: {'online.log': self.online_rule(path)}})
        path.write_bytes(b'')
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_dynamic_declaration_cannot_exempt_business_data_or_tracked_source(self):
        path = self.file('tracked.json', b'{"source":1}')
        self.git('add', 'tracked.json')
        self.git('commit', '-m', 'tracked JSON fixture')
        rule = self.online_rule(path, 'replace-cache')
        self.start({self.repo: {'tracked.json': rule}})
        path.write_bytes(b'{"source":2}')
        result = self.verify()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('tracked-source-cannot-be-dynamic' in item for item in result['failures']))
        rule['kind'] = 'business-data'
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_stale_or_wrong_owner_does_not_allow_dynamic_retention(self):
        path = self.file('online.log', b'original')
        rule = self.online_rule(path)
        self.start({self.repo: {'online.log': rule}})
        rule['dynamic']['observed_at_unix'] -= 1000
        with path.open('ab') as log:log.write(b'changed')
        self.assertEqual(self.verify()['status'], 'blocked')
        rule['dynamic']['observed_at_unix'] = time.time()
        rule['dynamic']['owner_signature'] = '0' * 64
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_dynamic_declaration_cannot_cover_a_directory(self):
        path = self.file('online/service.log', b'original')
        rule = self.online_rule(path)
        self.start({self.repo: {'online/': rule}})
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_dynamic_owner_is_rechecked_without_a_file_update(self):
        path = self.file('online.log', b'original')
        self.start({self.repo: {'online.log': self.online_rule(path)}})
        real_run = audit.git_state.run
        observations = 0
        def owner_exits(args, **kwargs):
            nonlocal observations
            if args[0] == 'ps':
                observations += 1
                if observations > 1:
                    return subprocess.CompletedProcess(args, 1, b'', b'')
            return real_run(args, **kwargs)
        with patch.object(audit.git_state, 'run', side_effect=owner_exits):
            result = self.verify()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('owner-missing-or-replaced' in item for item in result['failures']))

    def test_dynamic_cache_must_still_be_valid_json(self):
        path = self.file('cache/catalog.json', b'{"fixture":1}')
        self.start({self.repo: {'cache': self.keep('cache'),
                               'cache/catalog.json': self.online_rule(path, 'replace-cache')}})
        del self.review['classifications'][str(self.repo)]['cache/']
        path.write_bytes(b'invalid cache JSON')
        self.assertEqual(self.verify()['status'], 'blocked')

    def test_symlink_target_is_not_followed(self):
        target = self.root / 'other-project'
        self.file('private.bin', b'x' * 65536, root=target)
        (self.repo / 'linked').symlink_to(target, target_is_directory=True)
        before = self.start()
        entries = before['roots'][0]['entries']
        self.assertIn('linked', entries)
        self.assertNotIn('linked/private.bin', entries)

    def test_hardlink_allocations_are_deduplicated_across_roots(self):
        a = self.file('payload', b'x' * 65536)
        external = self.root / 'external'
        external.mkdir()
        os.link(a, external / 'same-payload')
        before = self.start(extras=[external])
        root_sum = sum(r['usage']['allocated_bytes'] for r in before['roots'])
        self.assertEqual(root_sum - before['totals']['allocated_bytes'], a.stat().st_blocks * 512)

    def test_new_registered_worktree_is_reported_as_unreviewed(self):
        self.start()
        self.git('worktree', 'add', '-b', 'dev/late', str(self.root / 'late'))
        result = self.verify()
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any('new-storage-root' in f for f in result['pending']))

    def test_scan_errors_cannot_pass(self):
        before = self.start()
        before['roots'][0]['errors'] = [{'path': str(self.repo / 'denied'), 'error': 'Permission denied'}]
        self.before_path.unlink()
        audit.save(self.before_path, before)
        self.review['before_sha256'] = audit.digest(self.before_path)
        self.assertEqual(self.verify()['status'], 'partial')

    def test_retained_external_source_cannot_lose_a_member(self):
        external = self.root / 'maintenance'
        self.file('src/main.py', root=external)
        unique = self.file('src/unique.py', root=external)
        self.start(extras=[external])
        unique.unlink()
        result = self.verify()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('protected-entry-missing' in f for f in result['failures']))

    def test_git_metadata_exemption_does_not_apply_to_arbitrary_copy(self):
        self.file('copy/unique.py')
        self.start({self.repo: {'copy/': self.keep('git-metadata')}})
        result = self.verify()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(any('not-a-project-git-metadata-path' in f for f in result['failures']))

    def test_retained_external_source_change_requires_review(self):
        external = self.root / 'maintenance'
        source = self.file('src/main.py', root=external)
        self.start(extras=[external])
        source.write_bytes(b'Unexpectedly replaced maintenance source')
        self.assertEqual(self.verify()['status'], 'partial')

    def test_inventory_from_different_repository_rejected(self):
        self.start()
        other = self.root / 'other'
        self.git('init', '-b', 'main', str(other), cwd=self.root)
        with self.assertRaisesRegex(ValueError, 'another repository'):
            audit.verify(other, self.review)

    def test_cli_capture_is_compressed_and_rejects_inside_output(self):
        script = str(Path(audit.__file__).resolve())
        result = subprocess.run([sys.executable, script, 'capture', '--repo', str(self.repo),
                                 '--output', str(self.before_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.before_path.read_bytes()[:2], b'\x1f\x8b')
        self.assertEqual(audit.load(self.before_path)['kind'], 'full-project-storage')
        inside = self.repo / 'invalid.json'
        result = subprocess.run([sys.executable, script, 'capture', '--repo', str(self.repo),
                                 '--output', str(inside)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(inside.exists())

    def test_no_capture_overwrite(self):
        self.start()
        with self.assertRaises(FileExistsError):
            audit.save(self.before_path, {})


if __name__ == '__main__':
    unittest.main()
