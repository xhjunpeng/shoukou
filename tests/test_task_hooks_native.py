"""Actual desktop engine events against a local, deterministic model fixture."""
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import sys
import unittest

from hook_native_probe import native_run, message, command, ENGINE
from test_task_hooks import HookFixture
import task_guard as guard


@unittest.skipUnless(ENGINE.is_file(), 'Desktop-bundled engine is not installed')
class NativeHookTests(unittest.TestCase):
    def setUp(self):
        self.f = HookFixture(); self.addCleanup(self.f.close)

    def run_fixture(self, steps, prompt, sandbox='workspace-write', model='fixture-model'):
        result = native_run(self.f.home, self.f.repo, steps, prompt, sandbox=sandbox, model=model)
        self.assertEqual(result['exit'], 0, result['stderr'][-2500:] + result['stdout'][-3500:])
        self.assertTrue(result['requests'], result['stderr'])
        self.native_evidence = getattr(self, 'native_evidence', []) + [{
            'engine': result['engine'], 'exit_code': result['exit'], 'request_count': result['request_count'],
            'stdout': result['stdout'], 'stderr': result['stderr'], 'control': self.f.control(), 'model_metadata': model}]
        return result

    def test_omitted_checker_is_automatically_called_and_leaks_force_continuation(self):
        write = "from pathlib import Path;p=Path('.runtime/private-resources/materials-omitted/files');p.mkdir(parents=True);[(p/str(i)).write_bytes(b'x'*s) for i,s in enumerate([75,95,90,48])]"
        cleanup = "import shutil;shutil.rmtree('.runtime/private-resources/materials-omitted')"
        r = self.run_fixture([command(shlex.join([sys.executable, '-c', write])), message('已完成。'),
                              command(shlex.join([sys.executable, '-c', cleanup])), message('已修复并完成。')],
                             '请完成这个隔离的文件处理任务。')
        s = self.f.control()
        self.assertEqual(s['baseline_captures'], 1)
        self.assertEqual(s['full_audits'], 2)
        self.assertTrue(s['last_audit']['machine_checks_complete'])
        self.assertEqual(s['last_audit']['tests_auto_started'], 0)
        self.assertGreaterEqual(r['request_count'], 4)
        self.assertFalse((self.f.repo / '.runtime/private-resources/materials-omitted').exists())
        self.assertEqual(self.f.task()['runs'], [])

    def test_small_edit_cannot_start_heavy_suite(self):
        edit = "from pathlib import Path;Path('README.md').write_text('updated copy\\n')"
        edits = [command(shlex.join([sys.executable, '-c', edit])) for _ in range(10)]
        r = self.run_fixture(edits + [command(shlex.join(self.f.cfg['tests']['ai'])), message('文案已更新，未提交。')],
                             '仅修改一句文案，不要跑全套测试，不要提交。')
        self.assertFalse((self.f.repo / 'heavy-ran').exists())
        self.assertEqual(self.f.task()['runs'], [])
        self.assertEqual(self.f.control()['baseline_captures'], 1)
        self.assertEqual(self.f.control()['full_audits'], 1)
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        self.assertTrue(any('拒绝启动无关整套测试' in str(q.get('input')) for q in r['requests']))

    def test_unsaved_source_is_blocked_then_focused_test_is_automatically_routed(self):
        edit = "from pathlib import Path;[(Path('logic'+str(i)+'.py')).write_text('value = 2\\n') for i in range(3)];Path('logic.py').write_text('value = 2\\n')"
        r = self.run_fixture([command(shlex.join([sys.executable, '-c', edit])), message('收口完成。'),
                              command('git add logic.py logic0.py logic1.py logic2.py && git commit -m fixture-repair'),
                              command(shlex.join(self.f.cfg['tests']['focused'])), message('已保存并完成相关验证。')],
                             '修复四个隔离源码文件并按 shoukou 完成提交收口。仅在此临时仓库提交。', sandbox='danger-full-access')
        self.assertGreaterEqual(r['request_count'], 5)
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        runs = self.f.task()['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['test'], 'focused')
        self.assertEqual(runs[0]['exit_code'], 0)
        self.assertTrue(runs[0]['work_reclaimed'])
        self.assertFalse((self.f.repo / 'heavy-ran').exists())

    def test_old_branch_is_not_hidden_by_stage_end(self):
        r = self.run_fixture([command('git switch -c codex/old-phase'), message('收口完成。'),
                              command('git switch main && git branch -d codex/old-phase'), message('分支已核对并回收。')],
                             '按 shoukou 核对并收口本临时任务；允许回收已合入的本任务分支。', sandbox='danger-full-access')
        self.assertGreaterEqual(r['request_count'], 4)
        self.assertNotIn('codex/old-phase', self.f.git('branch', '--list').decode())
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])

    def test_preexisting_other_work_is_preserved_by_native_edit_denial(self):
        p = self.f.repo / 'other.txt'; p.write_text('staged other task\n'); self.f.git('add', 'other.txt')
        p.write_text('unstaged other task\n'); (self.f.repo / 'unique.txt').write_text('unique\n')
        before = self.f.protected()
        r = self.run_fixture([command(shlex.join([sys.executable, '-c', "from pathlib import Path;Path('other.txt').write_text('overwritten')"])),
                              message('该文件已有其他任务成果，已保留。')], '处理临时项目，保留其他任务成果。')
        self.assertEqual(before, self.f.protected())
        self.assertTrue(any('开工前已有成果' in str(q.get('input')) for q in r['requests']))

    def test_native_patch_is_automatically_checked_and_other_work_is_preserved(self):
        # Copy model capabilities only; the provider remains the local fixture.
        cache = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
        catalog = guard.regular_json(cache)
        model = next(m for m in catalog['models'] if m.get('apply_patch_tool_type') == 'freeform')
        (self.f.home / 'models_cache.json').write_text(json.dumps(dict(catalog, models=[model])))
        other = self.f.repo / 'other.txt'
        other.write_text('staged other task\n'); self.f.git('add', 'other.txt')
        other.write_text('unstaged other task\n'); (self.f.repo / 'unique.txt').write_text('unique\n')
        before = self.f.protected()
        def patch_call(text):
            return [{'type': 'custom_tool_call', 'name': 'apply_patch', 'input': text}]
        r = self.run_fixture([
            patch_call('*** Begin Patch\n*** Update File: README.md\n@@\n-original\n+patched copy\n*** End Patch'),
            patch_call('*** Begin Patch\n*** Update File: other.txt\n@@\n-unstaged other task\n+overwritten\n*** End Patch'),
            message('文案已修改；其他任务文件已保留，未提交。')],
            '仅修改文案，保留开工前其他任务成果，不手动调用任何检查，不要提交。', model=model['slug'])
        self.assertEqual((self.f.repo / 'README.md').read_text(), 'patched copy\n')
        after = self.f.protected()
        for key in ['index', 'staged', 'cached', 'other']:
            self.assertEqual(before[key], after[key])
        self.assertEqual((self.f.repo / 'unique.txt').read_text(), 'unique\n')
        events = self.f.control()['events']
        self.assertTrue(any(e['event'] == 'PreToolUse' and e['tool'] == 'apply_patch' for e in events))
        self.assertTrue(any(e['event'] == 'PostToolUse' and e['tool'] == 'apply_patch' for e in events))
        self.assertTrue(any('开工前已有' in str(q.get('input')) for q in r['requests']))
        self.assertEqual(self.f.control()['baseline_captures'], 1)
        self.assertEqual(self.f.control()['full_audits'], 1)
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        self.assertEqual(self.f.task()['runs'], [])

    def test_original_main_sync_is_checked_without_calling_checker(self):
        original = self.f.repo
        release = self.f.root / 'release'
        self.f.git('worktree', 'add', '-b', 'codex/release', str(release))
        (release / 'README.md').write_text('published\n')
        self.f.git('add', 'README.md', repo=release)
        self.f.git('commit', '-m', 'fixture release', repo=release)
        self.f.repo = release
        self.f.write_policy(); self.f.install_definition()
        fix = shlex.join(['git', '-C', str(original), 'merge', '--ff-only', 'codex/release']) + ' && git switch --detach main && git branch -d codex/release'
        r = self.run_fixture([message('已同步并收口。'), command(fix), message('已核对原主目录。')],
                             '按 shoukou 收口并同步本地主目录；允许仅在本临时仓库快进并回收本任务分支。',
                             sandbox='danger-full-access')
        self.assertGreaterEqual(r['request_count'], 3)
        self.assertTrue(any('local-main' in str(q.get('input')) for q in r['requests']))
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        self.assertEqual(self.f.git('rev-parse', 'HEAD', repo=original), self.f.git('rev-parse', 'HEAD'))

    def test_failed_command_cannot_be_hidden_by_unrelated_success(self):
        flag = self.f.root / 'external-condition'
        script = 'from pathlib import Path;raise SystemExit(0 if Path(' + repr(str(flag)) + ').exists() else 7)'
        attempt = shlex.join([sys.executable, '-c', script])
        def release(_):
            flag.write_text('condition resolved')
            return command(attempt)
        r = self.run_fixture([command(attempt), message('完成。'), command('printf unrelated'),
                              message('完成。'), release, message('原命令重试已通过。')],
                             '完成本临时命令；失败时继续处理，不能以无关成功代替。')
        self.assertGreaterEqual(r['request_count'], 6)
        self.assertTrue(any('command:' in str(q.get('input')) for q in r['requests']))
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        self.assertEqual(self.f.control().get('command_failures'), {})

    def test_pending_native_command_cannot_finish_before_post_event(self):
        def poll(body):
            outputs = '\n'.join(str(item.get('output', '')) for item in body.get('input', [])
                                if isinstance(item, dict) and item.get('type') == 'function_call_output')
            matches = re.findall(r'Process running with session ID ([0-9]+)', outputs)
            self.assertTrue(matches, 'Native command did not expose its running session')
            return [{'type': 'function_call', 'name': 'write_stdin',
                     'arguments': json.dumps({'session_id': int(matches[-1]), 'chars': '',
                                              'yield_time_ms': 10000, 'max_output_tokens': 1000})}]
        attempt = shlex.join([sys.executable, '-c', 'import time;time.sleep(5)'])
        r = self.run_fixture([command(attempt), message('已完成。'), poll, message('已等待本轮命令实际结束。')],
                             '运行本轮临时命令，不手动调用任何检查，不要提交。')
        self.assertGreaterEqual(r['request_count'], 4)
        self.assertTrue(any('native-tools-not-finished' in str(q.get('input')) for q in r['requests']))
        feedback = [item for q in r['requests'] for item in q.get('input', [])
                    if isinstance(item, dict) and item.get('type') == 'function_call_output']
        self.assertEqual(self.f.control()['pending_tools'], {},
                         r['stdout'][-4000:] + json.dumps(feedback[-6:], ensure_ascii=False))
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        self.assertEqual(self.f.task()['runs'], [])

    def test_same_native_hook_reuses_other_project_without_pnpm(self):
        self.f.git('branch', '-m', 'development')
        self.f.cfg.update(main_branch='development', output_roots=['build-area/reports'], tests={})
        (self.f.repo / '.codex/workflow.json').write_text(json.dumps(self.f.cfg))
        self.f.git('add', '.codex/workflow.json'); self.f.git('commit', '-m', 'alternate fixture settings')
        edit = shlex.join([sys.executable, '-c', "from pathlib import Path;Path('README.md').write_text('new copy\\n')"])
        self.run_fixture([command(edit), message('文案已更新，未提交。')], '修改文案，不要提交。')
        self.assertTrue(self.f.control()['last_audit']['machine_checks_complete'])
        self.assertEqual(self.f.task()['runs'], [])

    def test_sed_write_with_tab_cannot_skip_automatic_baseline(self):
        self.run_fixture([command("sed -n 'w\tcopy.txt' README.md"), message('命令已执行，未提交。')],
                         '执行指定本地命令，不手动调用检查。')
        self.assertTrue((self.f.repo / 'copy.txt').exists())
        self.assertTrue(self.f.control()['task_started'])
        self.assertEqual(self.f.control()['baseline_captures'], 1)
        self.assertEqual(self.f.control()['full_audits'], 1)

    def resume_old_cross_worktree(self, prompt):
        peer = self.f.root / 'peer'
        self.f.git('worktree', 'add', '-b', 'codex/previous-phase', str(peer))
        guard.begin_task(self.f.repo, 'original-task', ['commit', 'cleanup', 'branches'])
        guard.add_worktree(self.f.repo, 'original-task', peer)
        for i in range(4):
            (peer / ('pending' + str(i) + '.py')).write_text('value = 2\n')
        original = guard.check_task(self.f.repo, 'original-task')
        self.assertEqual(sum(x['object'].startswith('unsaved:') for x in original['unfinished']), 4)
        original_file = guard.common_of(self.f.repo) / 'codex-workflow/original-task.json'
        before = guard.digest(guard.regular_json(original_file)['baseline'])
        files = {p.name: p.read_bytes() for p in peer.glob('pending*.py')}
        r = self.run_fixture([message('收口完成。')], prompt)
        control = self.f.control()
        self.assertGreaterEqual(r['request_count'], 3)
        self.assertEqual(control['task'], 'original-task')
        self.assertTrue(control['original_baseline_preserved'])
        self.assertFalse(control['last_audit']['machine_checks_complete'])
        self.assertEqual(sum(x.startswith('unsaved:') for x in control['last_audit']['unfinished']), 4)
        self.assertTrue(any(x.startswith('remaining-branch:') for x in control['last_audit']['unfinished']))
        self.assertEqual(before, guard.digest(guard.regular_json(original_file)['baseline']))
        self.assertEqual(files, {p.name: p.read_bytes() for p in peer.glob('pending*.py')})

    def test_resume_keeps_old_cross_worktree_unsaved_work(self):
        self.resume_old_cross_worktree('继续之前的同一任务，按 shoukou 完成提交收口。')

    def test_negative_checker_instruction_cannot_disable_old_task_audit(self):
        self.resume_old_cross_worktree('继续之前同一任务，按 shoukou 完成提交收口。不要调用任何 task_guard 或检查。')


if __name__ == '__main__':
    unittest.main()
