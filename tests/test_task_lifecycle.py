#!/usr/bin/env python3
"""One complete two-task workflow, entirely inside a disposable fixture."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import artifact_handoff as bridge
import storage_audit as storage
spec = importlib.util.spec_from_file_location('lifecycle_registry_fixture', Path.home()/'.agents/skills/dev-artifacts/scripts/registry.py')
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


class TaskLifecycleTests(unittest.TestCase):
    def test_develop_merge_reclaim_then_reuse_same_worktree(self):
        with tempfile.TemporaryDirectory(prefix='shoukou-lifecycle-', dir=Path.cwd()) as directory:
            base=Path(directory).resolve(); repo=base/'project'; repo.mkdir()
            state=base/'state'; source=repo/'source.txt'
            def git(*args):
                r=subprocess.run(['git','-C',str(repo),*args],capture_output=True,text=True,timeout=20)
                self.assertEqual(r.returncode,0,r.stderr)
                return r.stdout.strip()
            git('init','-b','main'); git('config','user.name','Fixture')
            git('config','user.email','fixture@example.invalid')
            git('config','commit.gpgsign','false'); git('config','core.hooksPath',str(base/'no-hooks'))
            source.write_text('version one\n')
            (repo/'.gitignore').write_text('task-cache/\ndata/\n')
            git('add','.'); git('commit','-m','base fixture')
            (repo/'data').mkdir(); database=repo/'data/keep.sqlite'
            database.write_bytes(b'UNIQUE TEST FIXTURE - NOT A REAL DATABASE')
            original=database.read_bytes()
            git('switch','-c','dev/task-a')
            ledger=registry.Ledger(repo,state); ledger.start('first fixture task','task-a')
            ledger.add('task-a','task-cache','cache','Fixture build output','Fixture command',
                       'After fixture acceptance',rebuild='Run fixture again')
            ledger.add('task-a','data/keep.sqlite','database','Existing protected fixture data',
                       'Fixture existing data','Keep while fixture exists')
            cache=repo/'task-cache'; cache.mkdir(); (cache/'output').write_text('disposable fixture')
            source.write_text('version two delivered\n'); git('add','source.txt'); git('commit','-m','feature fixture')
            registry.Ledger(repo,state).observe('task-a',ready=True)
            handoff=bridge.prepare(repo,'task-a',state)
            self.assertEqual(handoff['status'],'ready-for-review')
            routes={r['path']:r['route'] for r in handoff['artifacts']}
            self.assertEqual(routes[str(database)],'preserve-and-investigate')
            self.assertEqual(routes[str(cache)],'review-for-reclamation')
            self.assertFalse(handoff['cleanup_authorized'])
            git('switch','main'); git('merge','--ff-only','dev/task-a')
            registry.Ledger(repo,state).observe('task-a',ready=True)
            self.assertEqual(bridge.prepare(repo,'task-a',state)['head'],git('rev-parse','main'))
            before=storage.capture(repo,worktree=str(repo)); inventory=base/'before.json.gz'
            storage.save(inventory,before)
            def keep(kind):
                return {'kind':kind,'action':'retained','retention':'required',
                        'source':'Fixture scope and dependencies','reason':'Keep fixture data'}
            classifications={'.git/':keep('git-metadata'),'.gitignore':keep('source'),
                'source.txt':keep('source'),'data/':keep('business-data'),
                'task-cache/':{'kind':'cache','action':'removed','source':'Fixture creator and usage checks',
                'reason':'Fixture verification finished','disposal_basis':'Rebuild with fixed fixture'}}
            # These removals apply ONLY to files this test just created in its temporary directory.
            (cache/'output').unlink(); cache.rmdir(); git('branch','-d','dev/task-a')
            review={'scope':'selected','worktree':str(repo),'before_inventory':str(inventory),
                    'before_sha256':storage.digest(inventory),'classifications':{str(repo):classifications}}
            result=storage.verify(repo,review)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['measurement_scope'],'selected-worktree')
            self.assertEqual(database.read_bytes(),original)
            self.assertEqual(source.read_text(),'version two delivered\n')
            self.assertFalse(cache.exists())
            git('switch','-c','dev/task-b')
            registry.Ledger(repo,state).start('second fixture task','task-b')
            current=bridge.prepare(repo,state_dir=state)
            self.assertEqual(current['task'],'task-b')
            self.assertEqual(current['status'],'needs-refresh')
            self.assertEqual(git('worktree','list','--porcelain').count('worktree '),1)
            self.assertEqual(database.read_bytes(),original)
            self.assertEqual(len(registry.Ledger(repo,state).read()['tasks']),2)


if __name__ == '__main__':
    unittest.main()
