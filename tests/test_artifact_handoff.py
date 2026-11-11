#!/usr/bin/env python3
"""Integration fixtures only: no real project, database or cleanup operation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import artifact_handoff as bridge
import inspect_repo as inspect
import storage_audit as storage

REGISTRY = Path.home() / '.agents/skills/dev-artifacts/scripts/registry.py'
spec = importlib.util.spec_from_file_location('dev_artifacts_registry_fixture', REGISTRY)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


class ArtifactHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shoukou-bridge-', dir=Path.cwd())
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / 'project'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', str(self.home / 'no-hooks'))
        (self.repo / 'source.txt').write_text('source protected\n')
        (self.repo / '.gitignore').write_text('out/\n.runtime/\n')
        self.git('add', '.')
        self.git('commit', '-m', 'fixture base')
        self.state = self.home / 'codex/dev-artifacts'
        self.ledger = registry.Ledger(self.repo, self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, repo=None):
        r = subprocess.run(['git', '-C', str(repo or self.repo), *args], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def start(self, tid='task-a'):
        return self.ledger.start('Fixture '+tid, tid)

    def add(self, tid='task-a', path='out/batch', kind='cache', **kw):
        return self.ledger.add(tid, path, kind, 'Fixture purpose', 'Fixture test entry',
                               'After fixture acceptance', rebuild='Run isolated fixture', **kw)

    def ready(self, tid='task-a'):
        return self.ledger.observe(tid, ready=True)

    def plan(self, tid=None):
        return bridge.prepare(self.repo, tid, self.state)

    def edit(self, fn):
        data = self.ledger.read(); fn(data)
        self.ledger.file.write_text(json.dumps(data))

    def linked(self):
        path = self.home / 'linked-tree'
        self.git('worktree', 'add', '-b', 'dev/fixture', str(path))
        return path

    def storage_review(self, wt):
        before = storage.capture(self.repo, worktree=str(wt))
        p = self.home / 'before.json.gz'; storage.save(p, before)
        rules = {}
        common = Path(self.ledger.ctx['common'])
        for entry in before['roots']:
            root = Path(entry['path']); groups = {n.split('/')[0] for n in entry['entries']}
            rules[str(root)] = {}
            for name in groups:
                kind = 'git-metadata' if root == common or name == '.git' else 'source'
                rules[str(root)][name+('/' if (root/name).is_dir() else '')] = {
                    'kind':kind, 'action':'retained', 'retention':'required',
                    'source':'Fixture ownership', 'reason':'Fixture preservation'}
        return before, {'before_inventory':str(p), 'before_sha256':storage.digest(p),
                        'scope':'selected', 'worktree':str(wt), 'classifications':rules}

    def test_missing_ledger_is_not_empty_cleanup_and_does_not_write(self):
        self.assertEqual(self.plan()['status'], 'unregistered')
        self.assertFalse(self.state.exists())

    def test_active_task_requires_handoff(self):
        self.start(); self.ledger.observe('task-a')
        self.assertEqual(self.plan()['status'], 'needs-refresh')

    def test_task_without_observation_requires_refresh(self):
        self.start(); self.assertEqual(self.plan()['status'], 'needs-refresh')

    def test_ready_plan_preserves_ledger_and_source(self):
        self.start(); self.add(); (self.repo/'out/batch').mkdir(parents=True)
        (self.repo/'out/batch/log').write_text('fixture')
        self.ready(); raw = self.ledger.file.read_bytes()
        result = self.plan()
        self.assertEqual(result['status'], 'ready-for-review')
        self.assertEqual(result['artifacts'][0]['route'], 'review-for-reclamation')
        self.assertFalse(result['cleanup_authorized'])
        self.assertFalse(result['artifacts'][0]['cleanup_authorized'])
        self.assertEqual(self.ledger.file.read_bytes(), raw)
        self.assertEqual((self.repo/'source.txt').read_text(), 'source protected\n')
        self.assertTrue((self.repo/'out/batch/log').exists())

    def test_two_tasks_are_not_chosen_by_recency(self):
        self.start(); self.start('task-b'); self.ready(); self.ready('task-b')
        self.assertEqual(self.plan()['status'], 'needs-task-selection')
        self.assertEqual(self.plan('task-b')['task'], 'task-b')

    def test_paused_task_is_not_resumed(self):
        self.start(); self.ledger.state('task-a','paused','Fixture pause')
        self.assertEqual(self.plan('task-a')['status'], 'protected-paused')
        self.assertEqual(self.ledger.read()['tasks']['task-a']['state'],'paused')

    def test_other_worktree_task_is_not_adopted(self):
        wt = self.linked(); other = registry.Ledger(wt,self.state)
        other.start('other','other-task'); other.observe('other-task',ready=True)
        self.assertEqual(self.plan()['status'],'needs-task-selection')
        with self.assertRaises(ValueError): self.plan('other-task')

    def test_old_branch_not_silently_selected(self):
        self.start(); self.ready(); self.git('switch','-c','dev/next')
        self.assertEqual(self.plan()['status'],'needs-task-selection')
        self.assertEqual(self.plan('task-a')['status'],'needs-refresh')

    def test_new_commit_invalidates_handoff(self):
        self.start(); self.ready(); (self.repo/'source.txt').write_text('new source')
        self.git('add','source.txt'); self.git('commit','-m','next')
        self.assertIn('observation-version-mismatch',self.plan('task-a')['issues'])

    def test_stale_observation_does_not_pass(self):
        self.start(); self.ready()
        self.edit(lambda d:d['tasks']['task-a']['last_observation'].update(observed_at='2020-01-01T00:00:00+00:00'))
        self.assertEqual(self.plan()['status'],'needs-refresh')

    def test_preexisting_data_remains_protected(self):
        (self.repo/'out').mkdir(); (self.repo/'out/db.sqlite').write_text('unique fixture')
        self.start(); self.add(path='out/db.sqlite',kind='database'); self.ready()
        self.assertEqual(self.plan()['artifacts'][0]['route'],'preserve-and-investigate')

    def test_shared_resource_remains_protected(self):
        self.start(); self.add(shared=True); (self.repo/'out/batch').mkdir(parents=True); self.ready()
        self.assertEqual(self.plan()['artifacts'][0]['route'],'preserve-and-investigate')

    def test_external_reference_remains_protected(self):
        self.start(); self.add(path=str(self.home/'outside'),external=True)
        (self.home/'outside').mkdir(); self.ready()
        self.assertEqual(self.plan()['artifacts'][0]['route'],'preserve-and-investigate')

    def test_overlapping_tasks_are_independently_flagged(self):
        self.start(); self.add(); self.start('task-b'); self.add('task-b')
        (self.repo/'out/batch').mkdir(parents=True); self.ready()
        self.assertIn('overlapping-task-reference:task-b',self.plan('task-a')['artifacts'][0]['review_flags'])

    def test_absence_not_claimed_as_successful_deletion(self):
        self.start(); self.add(); self.ready()
        self.assertEqual(self.plan()['artifacts'][0]['route'],'absent-not-proof-of-our-cleanup')
        self.assertFalse(self.plan()['cleanup_authorized'])

    def test_unregistered_hints_are_kept(self):
        self.start(); (self.repo/'extra.txt').write_text('not trash'); self.ready()
        self.assertTrue(self.plan()['unregistered_candidates'])
        self.assertTrue((self.repo/'extra.txt').exists())

    def test_incomplete_measurement_is_protected(self):
        self.start(); self.add(); (self.repo/'out/batch').mkdir(parents=True); self.ready()
        self.edit(lambda d:d['tasks']['task-a']['last_observation']['artifacts'][0]['measurement'].update(complete=False))
        self.assertEqual(self.plan()['artifacts'][0]['route'],'preserve-and-investigate')

    def test_corrupt_ledger_is_not_reset(self):
        self.start(); self.ledger.file.write_text('broken')
        with self.assertRaises(ValueError): self.plan()
        self.assertEqual(self.ledger.file.read_text(),'broken')

    def test_other_repository_identity_rejected(self):
        self.start(); self.edit(lambda d:d.update(git_common_dir='/different/.git'))
        with self.assertRaises(ValueError): self.plan()

    def test_duplicate_json_keys_rejected(self):
        self.start(); self.ledger.file.write_text('{"tasks":{},"tasks":{}}')
        with self.assertRaises(ValueError): self.plan()

    def test_symlink_ledger_rejected_without_following(self):
        self.start(); original=self.ledger.file.read_bytes()
        target=self.home/'original.json'; target.write_bytes(original)
        self.ledger.file.unlink(); self.ledger.file.symlink_to(target)
        with self.assertRaises(ValueError): self.plan()
        self.assertEqual(target.read_bytes(),original)

    def test_artifact_observation_omission_rejected(self):
        self.start(); self.add(); self.ready()
        self.edit(lambda d:d['tasks']['task-a']['last_observation'].update(artifacts=[]))
        with self.assertRaises(ValueError): self.plan()

    def test_unsafe_artifact_root_rejected(self):
        self.start(); self.add(); self.ready()
        def tamper(d):
            item=next(iter(d['tasks']['task-a']['artifacts'].values())); item['path']=str(self.repo)
            d['tasks']['task-a']['last_observation']['artifacts'][0]['path']=str(self.repo)
        self.edit(tamper)
        with self.assertRaises(ValueError): self.plan()

    def test_invalid_task_identifier_rejected(self):
        self.start()
        with self.assertRaises(ValueError): self.plan('../task-a')

    def test_cli_reader_returns_real_status_and_never_deletes(self):
        self.start(); self.ready()
        cmd=[sys.executable,'-B',str(SCRIPTS/'artifact_handoff.py'),'--repo',str(self.repo),'--state-dir',str(self.state)]
        result=subprocess.run(cmd,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertFalse(json.loads(result.stdout)['cleanup_authorized'])
        bad=subprocess.run(cmd+['--delete'],capture_output=True,timeout=20)
        self.assertNotEqual(bad.returncode,0)
        self.assertTrue((self.repo/'source.txt').exists())

    def test_original_inspector_calls_bridge_for_exact_worktree(self):
        self.start(); self.ready(); self.linked()
        env=dict(os.environ,CODEX_HOME=str(self.state.parent))
        result=subprocess.run([sys.executable,'-B',str(SCRIPTS/'inspect_repo.py'),'--repo',str(self.repo),'--base','main','--offline','--json','--summary','--worktree',str(self.repo),'--artifacts','--artifact-task','task-a'],env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout)
        data=json.loads(result.stdout)
        self.assertEqual(len(data['worktrees']),1)
        self.assertEqual(data['artifact_handoff']['task'],'task-a')
        self.assertFalse(data['artifact_handoff']['cleanup_authorized'])

    def test_inspector_missing_ledger_not_silent_success(self):
        env=dict(os.environ,CODEX_HOME=str(self.state.parent))
        result=subprocess.run([sys.executable,'-B',str(SCRIPTS/'inspect_repo.py'),'--repo',str(self.repo),'--base','main','--offline','--json','--worktree',str(self.repo),'--artifacts'],env=env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,3,result.stdout)
        self.assertFalse(self.state.exists())

    def test_inspector_rejects_nonroot_selection(self):
        (self.repo/'child').mkdir()
        with self.assertRaises(ValueError): inspect.inventory(self.repo,'main',True,target=str(self.repo/'child'))

    def test_selected_storage_does_not_measure_other_worktrees(self):
        wt=self.linked()
        with patch.object(storage,'measure',wraps=storage.measure) as spy:
            before=storage.capture(self.repo,worktree=str(wt))
        self.assertNotIn(self.repo,[call.args[0] for call in spy.call_args_list])
        self.assertEqual(before['kind'],'selected-worktree-storage')
        self.assertEqual({r['path'] for r in before['roots']},{str(wt),self.ledger.ctx['common']})

    def test_selected_storage_verifies_with_exact_scope(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        result=storage.verify(self.repo,review)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['measurement_scope'],'selected-worktree')
        self.assertTrue((wt/'source.txt').exists())

    def test_selected_storage_cannot_claim_full_project(self):
        wt=self.linked(); before,review=self.storage_review(wt); review['scope']='all-task-branches'
        with self.assertRaises(ValueError): storage.verify(self.repo,review)

    def test_selected_storage_rejects_other_repository(self):
        foreign=self.home/'foreign'; foreign.mkdir(); self.git('init','-b','main',repo=foreign)
        with self.assertRaises(ValueError): storage.capture(self.repo,worktree=str(foreign))

    def test_selected_scope_not_expanded_by_new_unrelated_worktree(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        self.git('worktree','add','-b','dev/other',str(self.home/'other-tree'))
        result=storage.verify(self.repo,review)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['measurement_scope'],'selected-worktree')

    def test_full_project_capture_still_includes_all_worktrees(self):
        wt=self.linked(); before=storage.capture(self.repo)
        self.assertEqual(before['kind'],'full-project-storage')
        self.assertIn(str(self.repo),[r['path'] for r in before['roots']])
        self.assertIn(str(wt),[r['path'] for r in before['roots']])

    def test_selected_capture_cannot_omit_declared_root(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        before['roots']=[r for r in before['roots'] if r['path']!=str(wt)]
        p=Path(review['before_inventory']); p.unlink(); storage.save(p,before)
        review['before_sha256']=storage.digest(p)
        with self.assertRaises(ValueError): storage.verify(self.repo,review)

    def test_mixed_runtime_root_stays_protected_even_if_flag_omitted(self):
        self.start(); self.add(path='.runtime'); (self.repo/'.runtime').mkdir(); self.ready()
        self.edit(lambda d:d['tasks']['task-a']['last_observation']['artifacts'][0].update(review_flags=[]))
        self.assertEqual(self.plan()['artifacts'][0]['route'],'preserve-and-investigate')

    def test_database_mislabeled_cache_stays_protected(self):
        self.start(); self.add(path='out/test.sqlite'); (self.repo/'out').mkdir()
        (self.repo/'out/test.sqlite').write_text('fixture database marker'); self.ready()
        self.edit(lambda d:d['tasks']['task-a']['last_observation']['artifacts'][0].update(review_flags=[]))
        self.assertEqual(self.plan()['artifacts'][0]['route'],'preserve-and-investigate')

    def test_concurrent_ledger_change_is_rejected(self):
        self.start(); self.ready(); original=bridge.read_ledger; calls=[]
        def changing(path):
            data,sha=original(path); calls.append(sha)
            return data,sha if len(calls)==1 else 'changed'
        with patch.object(bridge,'read_ledger',side_effect=changing):
            with self.assertRaises(ValueError): self.plan()

    def test_selected_capture_cli_and_verify_do_not_expand_scope(self):
        wt=self.linked(); out=self.home/'cli-before.json.gz'
        result=subprocess.run([sys.executable,'-B',str(SCRIPTS/'storage_audit.py'),'capture','--repo',str(self.repo),'--worktree',str(wt),'--output',str(out)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(json.loads(result.stdout)['kind'],'selected-worktree-storage')
        self.assertNotIn(str(self.repo),[r['path'] for r in storage.load(out)['roots']])

    def test_selected_removed_fixture_is_verifiable(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        for rule in review['classifications'][str(wt)].values():
            rule.update(kind='copy',action='removed',disposal_basis='Isolated committed test fixture')
        self.git('worktree','remove',str(wt))
        admin=review['classifications'][self.ledger.ctx['common']]['worktrees/']
        admin.update(action='git-admin-retired',reason='Git retired the last linked fixture registration')
        result=storage.verify(self.repo,review)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['measurement_scope'],'selected-worktree')

    def test_git_retirement_cannot_hide_ordinary_missing_source(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        review['classifications'][str(wt)]['source.txt'].update(kind='git-metadata',action='git-admin-retired')
        result=storage.verify(self.repo,review)
        self.assertEqual(result['status'],'blocked')
        self.assertTrue(any('unsupported-git-admin-retirement' in s for s in result['failures']))

    def test_git_retirement_rejects_still_registered_worktree(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        review['classifications'][self.ledger.ctx['common']]['worktrees/'].update(action='git-admin-retired')
        result=storage.verify(self.repo,review)
        self.assertEqual(result['status'],'blocked')

    def test_git_retirement_requires_original_registration_evidence(self):
        wt=self.linked(); before,review=self.storage_review(wt)
        for rule in review['classifications'][str(wt)].values():
            rule.update(kind='copy',action='removed',disposal_basis='Isolated test fixture')
        self.git('worktree','remove',str(wt))
        review['classifications'][self.ledger.ctx['common']]['worktrees/'].update(action='git-admin-retired')
        del before['registered_worktrees']
        p=Path(review['before_inventory']);p.unlink();storage.save(p,before)
        review['before_sha256']=storage.digest(p)
        self.assertEqual(storage.verify(self.repo,review)['status'],'blocked')


if __name__ == '__main__':
    unittest.main()
