"""Real temporary Git/child-process counterexamples for the shared task guard."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/task_guard.py'
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location('task_guard_tested', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class TaskGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='task-guard-fixture-')
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / 'original'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'source.txt').write_text('baseline\n')
        (self.repo / '.gitignore').write_text('.runtime/\n')
        self.git('add', 'source.txt', '.gitignore')
        self.git('commit', '-m', 'baseline')
        guard.init_project(self.repo, output_roots=['.runtime'])
        cfg = self.config()
        cfg['tests'] = {'ok': [sys.executable, '-c', 'print("ok")'], 'other': [sys.executable, '-c', 'raise SystemExit(8)']}
        self.save_config(cfg)
        self.git('add', '.codex/workflow.json')
        self.git('commit', '-m', 'project workflow')

    def tearDown(self):
        folder = self.repo / '.git/codex-workflow'
        if folder.is_dir():
            for path in folder.glob('*.json'):
                if path.is_symlink():
                    continue
                for run in json.loads(path.read_text()).get('runs', []):
                    root = Path(run['root'])
                    if guard.run_group_active(run) is True:
                        try:
                            os.killpg(run['pgid'], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if root.name.startswith('codex-task-guard-') and root.exists():
                        shutil.rmtree(root)
        self.temp.cleanup()

    def git(self, *args, repo=None):
        return subprocess.check_output(['git', '-C', str(repo or self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def config(self):
        return json.loads((self.repo / '.codex/workflow.json').read_text())

    def save_config(self, cfg):
        (self.repo / '.codex/workflow.json').write_text(json.dumps(cfg))

    def begin(self, requirements=None, **kwargs):
        return guard.begin_task(self.repo, 'task', requirements, **kwargs)

    def check(self, **kwargs):
        return guard.check_task(self.repo, 'task', **kwargs)

    def objects(self, result):
        return {x['object'] for x in result['unfinished']}

    def state(self):
        return json.loads((self.repo / '.git/codex-workflow/task.json').read_text())

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), '--repo', str(self.repo), *args], capture_output=True, text=True)

    def test_optional_lock_wait_is_bounded(self):
        with guard.store(self.repo, 'holder'):
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, 'locked'):
                with guard.store(self.repo, 'task', wait_seconds=0.04):
                    self.fail('The held lock must remain exclusive')
            self.assertLess(time.monotonic() - started, 1)

    def test_optional_lock_wait_resumes_after_release(self):
        acquired, started, errors = threading.Event(), threading.Event(), []
        def waiter():
            started.set()
            try:
                with guard.store(self.repo, 'task', wait_seconds=0.5):
                    acquired.set()
            except Exception as error:
                errors.append(error)
        with guard.store(self.repo, 'holder'):
            thread = threading.Thread(target=waiter)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(acquired.wait(0.02))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(acquired.is_set())

    def interrupted_after_reclamation(self):
        self.begin(['cleanup'])
        argv = [sys.executable, '-c', 'print("recorded fixture command")']
        first = guard.run_task(self.repo, 'task', argv)['run']
        state = self.state()
        item = state['runs'][0]
        item['status'], item['runner_pid'] = 'running', 2147483647
        for key in ['exit_code', 'receipt', 'finished_at', 'violations', 'active_descendants', 'work_reclaimed', 'evidence']:
            item.pop(key, None)
        Path(first['receipt']['path']).unlink()
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        return argv, copy.deepcopy(item)

    def test_reclaimed_interruption_requires_same_current_successful_replay(self):
        argv, interrupted = self.interrupted_after_reclamation()
        key = 'run-resource:' + interrupted['id']
        self.assertIn(key, self.objects(self.check()))
        guard.run_task(self.repo, 'task', [sys.executable, '-c', 'print("unrelated")'])
        self.assertIn(key, self.objects(self.check()))
        replay = guard.run_task(self.repo, 'task', argv)['run']
        state = self.state()
        state['runs'][-1]['receipt']['sha256'] = 'invalid'
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        self.assertIn(key, self.objects(self.check()))
        state['runs'][-1]['receipt'] = replay['receipt']
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        checked = self.check()
        self.assertEqual(checked['state'], 'complete')
        self.assertEqual(checked['reconciled_run_resources'][0]['run_id'], interrupted['id'])
        self.assertEqual(self.state()['runs'][0], interrupted)
        self.assertNotIn('exit_code', self.state()['runs'][0])

    def test_live_parent_remaining_work_or_changed_identity_prevents_reconciliation(self):
        argv, interrupted = self.interrupted_after_reclamation()
        guard.run_task(self.repo, 'task', argv)
        key = 'run-resource:' + interrupted['id']
        state = self.state()
        state['runs'][0]['runner_pid'] = os.getpid()
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        self.assertIn(key, self.objects(self.check()))
        state['runs'][0]['runner_pid'] = interrupted['runner_pid']
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        work = Path(interrupted['root']) / 'work'
        work.mkdir()
        self.assertIn(key, self.objects(self.check()))
        work.rmdir()
        work.symlink_to(self.base / 'missing-target')
        self.assertIn(key, self.objects(self.check()))
        work.unlink()
        state['runs'][0]['root_identity'] = [0, 0]
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        self.assertIn(key, self.objects(self.check()))
        state['runs'][0]['root_identity'] = interrupted['root_identity']
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        self.assertEqual(self.check()['state'], 'complete')

    def test_unknown_process_group_never_completes_reclaimed_interruption(self):
        argv, interrupted = self.interrupted_after_reclamation()
        guard.run_task(self.repo, 'task', argv)
        normal = self.check()
        self.assertEqual(normal['state'], 'complete')
        self.core_evidence = {'normal': normal, 'unknown_groups': [], 'original_runs': []}
        for run in self.state()['runs']:
            root = Path(run['root'])
            self.core_evidence['original_runs'].append({
                'record': copy.deepcopy(run),
                'stdout': (root / 'evidence/stdout.log').read_text(),
                'stderr': (root / 'evidence/stderr.log').read_text()})
        key = 'run-resource:' + interrupted['id']
        for label, group in [('missing', None), ('null', None), ('zero', 0),
                             ('negative', -1), ('boolean', True), ('text', 'unknown')]:
            with self.subTest(group=label):
                state = self.state()
                state['runs'][0]['pgid'] = group
                if label == 'missing':
                    state['runs'][0].pop('pgid')
                original = copy.deepcopy(state['runs'][0])
                guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
                checked = self.check()
                self.core_evidence['unknown_groups'].append({'input': label, 'result': checked,
                                                             'original_record': original})
                self.assertIn(key, self.objects(checked))
                self.assertNotEqual(checked['state'], 'complete')
                self.assertEqual(checked['reconciled_run_resources'], [])
                self.assertEqual(self.state()['runs'][0], original)
        state = self.state()
        state['runs'][0] = interrupted
        guard.atomic(self.repo / '.git/codex-workflow/task.json', state)
        self.assertEqual(self.check()['state'], 'complete')

    def test_clean_task_and_preexisting_other_branch_do_not_require_global_cleanup(self):
        self.git('branch', 'other-task')
        self.begin()
        self.assertEqual(self.check()['state'], 'complete')
        self.assertEqual(self.git('rev-parse', 'other-task'), self.git('rev-parse', 'main'))

    def test_later_four_file_batch_in_old_worktree_is_not_hidden_by_previous_delivery(self):
        old = self.base / 'old-task'
        self.git('worktree', 'add', '-b', 'old-phase', str(old))
        self.begin()
        for name in ['one.ts', 'two.ts', 'three.ts', 'single-connection.test.ts']:
            (old / name).write_text('later independent batch\n')
        result = self.check()
        self.assertEqual(result['state'], 'continue')
        self.assertEqual(sum(x.startswith('unattributed-change:') for x in self.objects(result)), 4)
        self.assertTrue(any(x.startswith('cross-worktree-content:') for x in self.objects(result)))

    def test_original_main_cannot_be_replaced_by_clean_published_checkout(self):
        published = self.base / 'published'
        self.git('worktree', 'add', '-b', 'release', str(published))
        (published / 'source.txt').write_text('published\n')
        self.git('add', 'source.txt', repo=published)
        self.git('commit', '-m', 'published', repo=published)
        guard.begin_task(published, 'publish', ['merge', 'local-main'])
        result = guard.check_task(published, 'publish')
        self.assertEqual(result['original_main'], str(self.repo))
        self.assertIn('merge', self.objects(result))

    def test_old_owned_branch_survives_new_phase_cleanup_and_is_still_pending(self):
        self.git('branch', 'old-phase')
        self.begin(own_branches=['old-phase'])
        self.git('switch', '-c', 'new-phase')
        self.check(phase='save')
        self.git('switch', 'main')
        self.git('branch', '-d', 'new-phase')
        self.assertIn('remaining-branch:old-phase', self.objects(self.check()))
        self.assertNotIn('removed-protected-branch:new-phase', self.objects(self.check()))

    def test_existing_other_staged_unstaged_and_untracked_are_preserved(self):
        (self.repo / 'source.txt').write_text('staged other\n')
        self.git('add', 'source.txt')
        (self.repo / 'source.txt').write_text('unstaged other\n')
        (self.repo / 'other.txt').write_text('untracked other\n')
        before = {p: p.read_bytes() for p in [self.repo / 'source.txt', self.repo / 'other.txt', self.repo / '.git/index']}
        self.begin()
        self.assertEqual(self.check()['state'], 'complete')
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        (self.repo / 'source.txt').write_text('overwritten\n')
        self.assertTrue(any(x.startswith('protected-change:') for x in self.objects(self.check())))

    def test_own_existing_dirty_is_not_mislabeled_as_other_work(self):
        (self.repo / 'source.txt').write_text('this task\n')
        self.begin(own_paths=['source.txt'])
        self.assertIn('unsaved:' + str(self.repo) + ':source.txt', self.objects(self.check(phase='save')))
        self.git('add', 'source.txt'); self.git('commit', '-m', 'save task')
        self.assertEqual(self.check()['state'], 'complete')

    def test_resume_does_not_reset_baseline_or_shrink_requirements(self):
        self.begin(['commit', 'cleanup', 'tests'], tests=['ok'])
        before = copy.deepcopy(self.state()['baseline'])
        (self.repo / 'source.txt').write_text('new\n')
        self.begin()
        self.assertEqual(self.state()['baseline'], before)
        self.assertEqual(self.state()['selected_tests'], ['ok'])
        with self.assertRaises(ValueError):
            self.begin(['commit'])
        with self.assertRaises(ValueError):
            self.begin(own_paths=['source.txt'])

    def test_new_worktree_and_changed_protected_branch_are_detected(self):
        self.git('branch', 'protected')
        self.begin()
        extra = self.base / 'extra'
        self.git('worktree', 'add', '-b', 'new-work', str(extra))
        result = self.check()
        self.assertIn('new-worktree:' + str(extra), self.objects(result))
        guard.add_worktree(self.repo, 'task', extra)
        (extra / 'source.txt').write_text('task on extra\n')
        self.assertIn('unsaved:' + str(extra) + ':source.txt', self.objects(self.check()))

    def test_run_uses_system_temp_and_success_requires_only_selected_tests(self):
        self.begin(['tests', 'cleanup'], tests=['ok'])
        run = guard.run_task(self.repo, 'task', None, 'ok')
        self.assertEqual(run['state'], 'complete')
        root = Path(run['run']['root'])
        self.assertFalse(root.is_relative_to(self.repo))
        self.assertFalse((root / 'work').exists())
        self.assertTrue((root / 'evidence/result.json').is_file())
        self.assertEqual(self.check()['state'], 'complete')

    def test_nonzero_zero_leakage_is_not_erased_by_unrelated_success(self):
        self.begin(['cleanup'])
        failed = guard.run_task(self.repo, 'task', [sys.executable, '-c', 'raise SystemExit(9)'])
        self.assertEqual(failed['run']['exit_code'], 9)
        guard.run_task(self.repo, 'task', [sys.executable, '-c', 'pass'])
        self.assertTrue(any(x.startswith('run:') for x in self.objects(self.check())))
        (self.repo / 'source.txt').write_text('different candidate\n')
        self.assertTrue(any(x.startswith('run:') for x in self.objects(self.check())))

    def test_same_command_current_candidate_success_can_resolve_its_failure(self):
        self.begin(['cleanup'])
        marker = self.base / 'ready'
        command = [sys.executable, '-c', 'import pathlib,sys;sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 6)', str(marker)]
        guard.run_task(self.repo, 'task', command)
        marker.write_text('ready')
        self.assertEqual(guard.run_task(self.repo, 'task', command)['state'], 'complete')
        self.assertEqual(self.check()['state'], 'complete')

    def test_short_system_temp_supports_real_unix_socket(self):
        long = self.base / ('long-' + 'x' * 140)
        long.mkdir()
        with socket.socket(socket.AF_UNIX) as server:
            with self.assertRaises(OSError): server.bind(str(long / '10323.pipe'))
        self.begin(['cleanup'])
        command = [sys.executable, '-c', 'import os,pathlib,socket; p=pathlib.Path(os.environ["TMPDIR"])/"tsx-501"; p.mkdir(); s=socket.socket(socket.AF_UNIX); s.bind(str(p/"10323.pipe")); s.close(); print(p)']
        run = guard.run_task(self.repo, 'task', command)
        self.assertEqual(run['state'], 'complete')
        root = Path(run['run']['root'])
        self.assertEqual(root.parent, Path('/tmp').resolve())
        self.assertFalse((root / 'work').exists())
        self.assertEqual(self.check()['state'], 'complete')

    def test_reclaim_exact_stopped_run_preserves_failure_receipt_then_same_command_can_pass(self):
        self.begin(['cleanup'])
        ready = self.base / 'ready'
        command = [sys.executable, '-c', 'import os,pathlib,sys; (pathlib.Path(os.environ["TMPDIR"])/"owned").write_text("fixture"); print("fixture only; no external resources",file=sys.stderr); sys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 9)', str(ready)]
        failed = guard.run_task(self.repo, 'task', command)['run']
        root = Path(failed['root']); receipt = Path(failed['receipt']['path']); original = receipt.read_bytes()
        evidence = root / 'evidence/stderr.log'; sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
        result = self.cli('reclaim-run', '--task', 'task', '--run', failed['id'], '--evidence', str(evidence), '--sha256', sha,
                          '--reason', 'Read original stderr: only this fixture file was created; no external resources; child process group has stopped')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / 'work').exists())
        self.assertEqual(receipt.read_bytes(), original)
        recorded = self.state()['runs'][0]
        self.assertEqual(recorded['exit_code'], 9)
        self.assertEqual(recorded['reclamations'][0]['evidence']['sha256'], sha)
        self.assertTrue(any(x.startswith('run:') for x in self.objects(self.check())))
        ready.write_text('ready')
        self.assertEqual(guard.run_task(self.repo, 'task', command)['state'], 'complete')
        self.assertEqual(self.check()['state'], 'complete')
        self.assertEqual(receipt.read_bytes(), original)

    def test_reclaim_rejects_unknown_active_tampered_or_replaced_resources(self):
        self.begin(['cleanup'])
        command = [sys.executable, '-c', 'import os,pathlib,sys; (pathlib.Path(os.environ["TMPDIR"])/"keep").write_text("keep"); print("fixture stopped",file=sys.stderr);sys.exit(3)']
        failed = guard.run_task(self.repo, 'task', command)['run']
        root = Path(failed['root']); work = root / 'work'; evidence = root / 'evidence/stderr.log'
        sha = hashlib.sha256(evidence.read_bytes()).hexdigest()
        def reclaim(run_id=failed['id'], proof=evidence, proof_sha=sha):
            return guard.reclaim_run(self.repo, 'task', run_id, proof, proof_sha, 'Read original fixture result; no external resources remain')
        with self.assertRaises(ValueError): reclaim(run_id='not-a-recorded-run')
        with self.assertRaises(ValueError): reclaim(proof_sha='0' * 64)
        with patch.object(guard, 'group_active', return_value=True):
            with self.assertRaises(ValueError): reclaim()
        with patch.object(guard, 'group_active', return_value=None):
            with self.assertRaises(ValueError): reclaim()
        path = self.repo / '.git/codex-workflow/task.json'; original_state = self.state()
        state = copy.deepcopy(original_state); state['runs'][0]['status'] = 'interrupted'; path.write_text(json.dumps(state))
        with self.assertRaises(ValueError): reclaim()
        state = copy.deepcopy(original_state); state['runs'][0]['pgid'] = None; path.write_text(json.dumps(state))
        with self.assertRaises(ValueError): reclaim()
        path.write_text(json.dumps(original_state))
        inside = work / 'proof'; inside.write_text('must not delete evidence')
        with self.assertRaises(ValueError): reclaim(proof=inside, proof_sha=hashlib.sha256(inside.read_bytes()).hexdigest())
        receipt = Path(failed['receipt']['path']); original_receipt = receipt.read_bytes(); receipt.write_text('tampered')
        with self.assertRaises(ValueError): reclaim()
        receipt.write_bytes(original_receipt)
        work.rename(root / 'evidence/original-work'); work.mkdir(); (work / 'keep').write_text('replacement')
        with self.assertRaises(ValueError): reclaim()
        self.assertEqual((work / 'keep').read_text(), 'replacement')
        shutil.rmtree(work); work.symlink_to(root / 'evidence/original-work', target_is_directory=True)
        with self.assertRaises(ValueError): reclaim()
        self.assertEqual((work / 'keep').read_text(), 'keep')

    def test_private_material_leak_is_caught_without_removing_preexisting_resources(self):
        materials = self.repo / '.runtime/private-resources/materials-existing'
        materials.mkdir(parents=True); original = materials / 'protected.txt'; original.write_text('protected')
        self.begin(['cleanup'])
        code = 'from pathlib import Path; p=Path(".runtime/private-resources/materials-new");p.mkdir();(p/"leak").write_text("new")'
        run = guard.run_task(self.repo, 'task', [sys.executable, '-c', code])
        self.assertEqual(run['state'], 'continue')
        self.assertEqual(original.read_text(), 'protected')
        self.assertTrue((self.repo / '.runtime/private-resources/materials-new/leak').exists())
        self.assertTrue(any(x.startswith('output:') for x in run['run']['violations']))

    def test_other_registered_worktree_outputs_cannot_escape_cleanup_check(self):
        cfg = self.config(); cfg['output_roots'] = ['.runtime/private-resources']; self.save_config(cfg)
        self.git('add', '.codex/workflow.json'); self.git('commit', '-m', 'narrow explicit output scope')
        other = self.base / 'other'
        self.git('worktree', 'add', '-b', 'other-task', str(other))
        root = other / '.runtime/private-resources'
        protected = root / 'materials-existing/protected.txt'
        protected.parent.mkdir(parents=True); protected.write_text('other task original')
        self.begin(['commit', 'cleanup'])
        self.assertEqual(self.check()['state'], 'complete')
        added = [root / 'materials-new' / name for name in ['one', 'two', 'three', 'four']]
        for path in added:
            path.parent.mkdir(exist_ok=True); path.write_text('fixture test leak')
        result = self.check()
        expected = {'output:' + str(other) + ':' + path.relative_to(other).as_posix() for path in added}
        self.assertTrue(expected <= self.objects(result))
        self.assertEqual(self.cli('check', '--task', 'task').returncode, 3)
        self.assertEqual(protected.read_text(), 'other task original')
        self.assertTrue(all(path.is_file() for path in added))
        for path in added: path.unlink()  # Only this fixture's known files, never tool cleanup.
        self.assertEqual(self.check()['state'], 'complete')
        protected.write_text('changed independently')
        self.assertIn('output:' + str(other) + ':.runtime/private-resources/materials-existing/protected.txt', self.objects(self.check()))

    def test_all_worktree_output_names_are_distinct_and_run_checks_other_root(self):
        other = self.base / 'other'
        self.git('worktree', 'add', '-b', 'other-task', str(other))
        self.begin(['cleanup'])
        for tree in [self.repo, other]:
            root = tree / '.runtime'; root.mkdir(); (root / 'same-name').write_text(str(tree))
        result = self.check()
        expected = {'output:' + str(tree) + ':.runtime/same-name' for tree in [self.repo, other]}
        self.assertTrue(expected <= self.objects(result))
        command = [sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("run leak")', str(other / '.runtime/from-run')]
        run = guard.run_task(self.repo, 'task', command)
        self.assertEqual(run['state'], 'continue')
        self.assertIn('output:' + str(other) + ':.runtime/from-run', run['run']['violations'])

    def test_new_and_unavailable_worktree_outputs_stay_visible(self):
        self.begin(['cleanup'])
        other = self.base / 'new-tree'
        self.git('worktree', 'add', '-b', 'other-task', str(other))
        root = other / '.runtime'; root.mkdir(); artifact = root / 'new'; artifact.write_text('fixture')
        result = self.check()
        self.assertIn('new-worktree:' + str(other), self.objects(result))
        self.assertIn('output:' + str(other) + ':.runtime/new', self.objects(result))
        # A separate baseline knows this exact resource, then the directory vanishes.
        guard.begin_task(self.repo, 'missing', ['cleanup'])
        moved = self.base / 'temporarily-moved'; other.rename(moved)
        result = guard.check_task(self.repo, 'missing')
        self.assertIn('unavailable-worktree:' + str(other), self.objects(result))
        self.assertIn('removed-protected-output:' + str(other) + ':.runtime/new', self.objects(result))
        self.assertIn('uncovered-output-root:' + str(other) + ':.runtime', self.objects(result))
        self.assertEqual((moved / '.runtime/new').read_text(), 'fixture')
        moved.rename(other)
        self.assertEqual(guard.check_task(self.repo, 'missing')['state'], 'complete')

    def test_legacy_output_baseline_is_not_silently_extended(self):
        other = self.base / 'other'; self.git('worktree', 'add', '-b', 'other-task', str(other))
        self.begin(['cleanup'])
        path = self.repo / '.git/codex-workflow/task.json'
        state = self.state(); state['baseline']['outputs'] = {'files': {}, 'uncovered': []}
        path.write_text(json.dumps(state))
        self.assertIn('uncovered-baseline-output-root:' + str(other) + ':.runtime', self.objects(self.check()))
        self.assertNotIn('scope', self.state()['baseline']['outputs'])

    def test_retention_decision_is_current_version_bound_and_has_review_event(self):
        self.begin(['cleanup'])
        output = self.repo / '.runtime/result'; output.parent.mkdir(); output.write_text('first')
        item = next(x for x in self.check()['unfinished'] if x['object'].startswith('output:'))
        raw = self.base / 'runner-original.txt'; raw.write_text('original command output')
        decision = {**{k: item[k] for k in ['object', 'version']}, 'kind': 'retained-output', 'reason': 'Preserve exact original result',
                    'retention': 'Current verification receipt', 'review_event': 'After the next accepted release',
                    'evidence': {'path': str(raw), 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest()}}
        path = self.base / 'decision.json'; path.write_text(json.dumps(decision))
        self.assertEqual(self.check(decision_paths=[path])['state'], 'complete')
        output.write_text('changed')
        self.assertEqual(self.check()['state'], 'continue')
        decision.pop('review_event'); path.write_text(json.dumps(decision))
        with self.assertRaises(ValueError): self.check(decision_paths=[path])

    def test_empty_or_oversized_output_coverage_never_passes_cleanup(self):
        cfg = self.config(); cfg['output_roots'] = []; self.save_config(cfg)
        self.begin(['cleanup'])
        self.assertIn('uncovered-output-roots', self.objects(self.check()))

    def test_output_limit_is_uncovered_not_silently_truncated(self):
        root = self.repo / '.runtime'; root.mkdir()
        for name in ['one', 'two']: (root / name).write_text(name)
        with patch.object(guard, 'LIMIT', 1):
            self.begin(['cleanup'])
            self.assertIn('uncovered-output-root:' + str(self.repo) + ':.runtime', self.objects(self.check()))

    def test_log_limit_returns_continue_and_preserves_raw_bounded_evidence(self):
        self.begin(['cleanup'])
        with patch.object(guard, 'MAX_LOG_BYTES', 128):
            run = guard.run_task(self.repo, 'task', [sys.executable, '-c', 'print("x"*4096)'])
        self.assertEqual(run['state'], 'continue')
        self.assertLessEqual((Path(run['run']['root']) / 'evidence/stdout.log').stat().st_size, 128)

    def test_replaced_work_directory_is_not_deleted(self):
        self.begin(['cleanup'])
        code = 'import os,pathlib; w=pathlib.Path(os.environ["TMPDIR"]); w.rename(w.parent/"evidence"/"old-work");w.mkdir();(w/"keep").write_text("keep")'
        with self.assertRaises(ValueError): guard.run_task(self.repo, 'task', [sys.executable, '-c', code])
        run = self.state()['runs'][-1]
        self.assertEqual((Path(run['root']) / 'work/keep').read_text(), 'keep')
        self.assertEqual(run['status'], 'interrupted')

    def test_config_drift_state_symlink_and_concurrent_state_writer_rejected(self):
        self.begin()
        with guard.store(self.repo, 'task'):
            other = self.cli('check', '--task', 'task')
            self.assertEqual(other.returncode, 2)
        cfg = self.config(); cfg['output_roots'].append('new-output'); self.save_config(cfg)
        with self.assertRaises(ValueError): self.check()
        state = self.repo / '.git/codex-workflow/task.json'
        protected = self.base / 'protected.json'; protected.write_text('do not touch')
        state.unlink(); state.symlink_to(protected)
        with self.assertRaises(ValueError): self.check()
        self.assertEqual(protected.read_text(), 'do not touch')

    def test_force_killed_observer_leaves_work_and_pending_run_resources(self):
        self.begin(['cleanup'])
        child = subprocess.Popen([sys.executable, '-B', str(SCRIPT), '--repo', str(self.repo), 'run', '--task', 'task', '--', sys.executable, '-c', 'import time;time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.state()['runs'] and self.state()['runs'][-1].get('pgid'): break
                time.sleep(.03)
            self.assertTrue(self.state()['runs'][-1]['pgid'])
            child.kill(); child.wait(timeout=3)
            run = self.state()['runs'][-1]
            self.assertTrue((Path(run['root']) / 'work').exists())
            self.assertTrue(any(x.startswith('run-resource:') for x in self.objects(self.check())))
        finally:
            if child.poll() is None: child.kill(); child.wait()

    def test_stopping_condition_is_persisted_for_only_its_exact_object_version(self):
        self.begin(['cleanup'])
        output = self.repo / '.runtime/waiting'; output.parent.mkdir(); output.write_text('waiting')
        item = self.check()['unfinished'][0]
        original = self.base / 'external-refusal.log'; original.write_text('Fixture external reviewer refusal; preserve the pending artifact\n')
        stops = {item['object']: {'version': item['version'], 'kind': 'external-dependency',
                 'attempt': 'Fixture request to external reviewer', 'result': 'Fixture reviewer declined',
                 'release_condition': 'Reviewer provides a verified release record',
                 'evidence': {'path': str(original), 'sha256': hashlib.sha256(original.read_bytes()).hexdigest()}}}
        path = self.base / 'stops.json'; path.write_text(json.dumps(stops))
        self.assertEqual(self.check(stop_paths=[path])['state'], 'blocked')
        self.assertEqual(self.check()['state'], 'blocked')
        output.write_text('a newer version')
        self.assertEqual(self.check()['state'], 'continue')

    def test_init_preserves_existing_config_and_does_not_guess_outputs_or_deployment(self):
        original = (self.repo / '.codex/workflow.json').read_bytes()
        self.assertEqual(guard.init_project(self.repo)['state'], 'existing-configuration-preserved')
        self.assertEqual((self.repo / '.codex/workflow.json').read_bytes(), original)
        other = self.base / 'new-project'; other.mkdir()
        self.git('init', '-b', 'master', repo=other)
        (other / 'readme').write_text('project')
        self.git('add', 'readme', repo=other)
        self.git('commit', '-m', 'first', repo=other)
        guard.init_project(other)
        cfg = json.loads((other / '.codex/workflow.json').read_text())
        self.assertEqual(cfg['main_branch'], 'master')
        self.assertEqual(cfg['output_roots'], [])
        self.assertNotIn('deployment', cfg)


if __name__ == '__main__':
    unittest.main()
