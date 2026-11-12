#!/usr/bin/env python3
"""Synchronous native Codex events: cheap per-operation guards, one phase audit.

This is a local guardrail, not an OS sandbox or a grant of Git/deploy authority.
No test command is started by Stop. Tests are only routed when requested by the
agent, and only exact configured commands acquire runner-backed evidence.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import stat

sys.dont_write_bytecode = True


def bootstrap_integrity():
    """Use stdlib only until all runtime dependencies have been checked."""
    cli = argparse.ArgumentParser(add_help=False)
    cli.add_argument('--policy', required=True)
    cli.add_argument('--policy-sha256')
    args, _ = cli.parse_known_args()

    def file(path):
        path = Path(os.path.abspath(path))
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('Symbolic link in runtime policy or source')
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_BOOT_FILE:
            raise ValueError('Runtime policy/source is not a bounded regular file')
        return path.read_bytes()

    data = file(args.policy)
    if args.policy_sha256 and hashlib.sha256(data).hexdigest() != args.policy_sha256:
        raise ValueError('Policy hash mismatch')
    policy = json.loads(data)
    pins = policy.get('pinned_files', [])
    for pin in pins:
        if hashlib.sha256(file(pin['path'])).hexdigest() != pin['sha256']:
            raise ValueError('Protected hook/checker changed; review the exact new version before continuing')
    if policy.get('runtime_release'):
        root = Path(policy['runtime_release'])
        names = {'task_hooks.py', 'task_guard.py', 'closeout_progress.py', 'readonly_query.py', 'manage_task_hooks.py'}
        raw_manifest = file(root / 'RUNTIME.json')
        manifest = json.loads(raw_manifest)
        expected = {str(root / n): manifest.get('files', {}).get(n) for n in names}
        actual = {p['path']: p['sha256'] for p in pins}
        if (hashlib.sha256(raw_manifest).hexdigest() != policy.get('runtime_manifest_sha256')
                or manifest.get('format') != 'codex-task-hook-runtime-v1' or actual != expected
                or manifest.get('version') != root.name
                or {p.name for p in root.iterdir()} != names | {'RUNTIME.json'}
                or Path(__file__).absolute().parent != root):
            raise ValueError('Frozen runtime manifest or dependency set changed')


MAX_BOOT_FILE = 2 * 1024 * 1024
if __name__ == '__main__':
    try:
        bootstrap_integrity()
    except (ValueError, OSError, KeyError, TypeError) as error:
        try:
            event = json.load(sys.stdin) if not sys.stdin.isatty() else {}
        except (ValueError, OSError):
            event = {}
        reason = '自动检查执行失败，不能当作通过：' + str(error)
        if event.get('hook_event_name') == 'PreToolUse':
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                  'permissionDecision': 'deny', 'permissionDecisionReason': reason}}, ensure_ascii=False))
            raise SystemExit(0)
        if event.get('hook_event_name') == 'Stop':
            print(json.dumps({'continue': False, 'stopReason': reason}, ensure_ascii=False))
            raise SystemExit(0)
        print(reason, file=sys.stderr)
        raise SystemExit(2)
    # -I keeps workspace/PYTHONPATH modules out of the trusted bootstrap.
    sys.path.insert(0, str(Path(__file__).absolute().parent))
import task_guard as guard
from readonly_query import readonly_command, segments
import manage_task_hooks as management

MAX_INPUT = 2 * 1024 * 1024
EVENTS = ['UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'Interrupt']
LOW_IMPACT = {'.md', '.txt', '.css', '.scss'}
READ_COMMANDS = {'pwd', 'ls', 'cat', 'rg', 'head', 'tail', 'wc', 'which', 'true', 'printf'}
READ_GIT = {'status', 'diff', 'log', 'show', 'ls-files', 'ls-tree', 'rev-parse', 'show-ref', 'for-each-ref', 'merge-base'}
BROAD_SCRIPTS = {'test', 'check', 'test:ai', 'test:integration', 'test:e2e', 'test:isolated'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def execute(*argv, cwd):
    return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=8,
                          env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))


def repo_of(cwd):
    result = execute('git', '--no-optional-locks', 'rev-parse', '--show-toplevel', cwd=cwd)
    return Path(os.fsdecode(result.stdout).strip()).resolve() if result.returncode == 0 else None


def tokens(command):
    try:
        return shlex.split(command)
    except ValueError:
        return []


def is_readonly(command):
    return readonly_command(command) is not None


def patch_paths(command, cwd):
    result = []
    for value in re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$', command, re.M):
        path = Path(value)
        result.append(Path(os.path.abspath(path if path.is_absolute() else Path(cwd) / path)))
    return result


def paths_if_config(command, repo):
    return patch_paths(command, str(repo)) == [repo / '.codex/workflow.json'] and '*** Delete File:' not in command and '*** Move to:' not in command


def changed_paths(repo):
    rows = guard.git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all').stdout.split(b'\0')
    result, i = [], 0
    while i < len(rows):
        row = rows[i]; i += 1
        if row:
            result.append(os.fsdecode(row[3:]))
            if b'R' in row[:2] or b'C' in row[:2]:
                i += 1
    return result


def low_impact(path):
    p = Path(path)
    return p.suffix.lower() in LOW_IMPACT and p.name not in {'AGENTS.md', 'CLAUDE.md', 'SKILL.md'} and '.codex' not in p.parts


def deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def closeout_requirements(prompt):
    # Scope selection is conservative metadata, never permission to commit or
    # deploy. Questions/plans and explicit pauses cannot become closeout intent.
    parts = re.split(r'[。！？\n;；，,]', prompt)
    positive, veto = [], set()
    for part in parts:
        if re.search(r'为什么|怎么|如何|仅咨询|只读|先定.*方案|先不.*实现', part, re.I):
            continue
        for verb, requirement in [('shoukou|收口|提交', 'commit'), ('合并', 'merge'),
                                  ('推送', 'push'), ('同步', 'local-main'), ('上线|部署', 'deploy')]:
            if re.search(r'(?:先不|不要|暂不|无需|不必|不能|勿|别)\s*(?:再|进行|执行|做|完成|安排)?\s*(?:' + verb + ')', part, re.I):
                veto.add(requirement)
        positive.append(part)
    actions = '。'.join(positive)
    requested = re.search(r'(?:按|用|执行|遵循|进行|完成).{0,24}(?:shoukou|收口)|完成提交|提交.*合并|完成.*推送|^\s*(?:\$?shoukou|收口)\s*$', actions, re.I)
    if not requested or 'commit' in veto:
        return set()
    result = {'commit', 'branches', 'cleanup'}
    for word, requirement in [('合并', 'merge'), ('推送', 'push'), ('同步.*本地|本地.*同步', 'local-main'), ('上线|部署', 'deploy')]:
        if requirement not in veto and re.search(word, actions):
            result.add(requirement)
    return result


class Hooks:
    def __init__(self, policy_path, state_root):
        self.policy_path = guard.no_links(Path(policy_path).absolute())
        self.policy = guard.regular_json(self.policy_path)
        if self.policy.get('schema_version') != 1:
            raise ValueError('Unsupported hook policy')
        self.state_root = guard.no_links(Path(state_root).absolute())

    def enabled(self, repo):
        if repo is None:
            return False
        if self.policy.get('scope') == 'configured':
            return True
        return str(repo) in self.policy.get('pilot_roots', [])

    def handle(self, event):
        start = time.monotonic()
        name = event.get('hook_event_name')
        if name not in EVENTS:
            return {}
        repo = repo_of(event.get('cwd', os.getcwd()))
        if not self.enabled(repo):
            return {}
        guard.no_links(self.state_root)
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        session = event.get('session_id')
        if not isinstance(session, str) or not session:
            raise ValueError('Native session ID missing')
        key = guard.digest([str(repo), session])[:32]
        path = self.state_root / (key + '.json')
        lock = self.state_root / (key + '.lock')
        fd = os.open(guard.no_links(lock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            state = guard.regular_json(path) if path.exists() else {
                'schema_version': 1, 'repo': str(repo), 'session_id': session,
                'task': 'native-' + key[:24], 'task_started': False,
                'turn': None, 'mutations': 0, 'full_audits': 0, 'baseline_captures': 0,
                'requirements': ['cleanup'], 'owned_paths': [], 'events': [], 'pending_tools': {},
            }
            if state['repo'] != str(repo) or state['session_id'] != session:
                raise ValueError('Native task binding changed')
            bound = self.policy.get('task_bindings', {}).get(session + ':' + str(repo))
            if bound and not state['task_started']:
                with guard.store(repo, bound) as (_, taskfile):
                    original = guard.regular_json(taskfile)
                if original['repo'] != str(repo):
                    raise ValueError('Existing task belongs to another checkout')
                state.update(task=bound, task_started=True, requirements=original['requirements'])
            self.verify_integrity()
            output = self.dispatch(event, repo, state)
            state['events'].append({'event': name, 'tool': event.get('tool_name'),
                                    'tool_use_id': event.get('tool_use_id'),
                                    'elapsed_ms': round((time.monotonic() - start) * 1000, 2)})
            state['events'] = state['events'][-128:]
            guard.atomic(path, state)
            return output
        finally:
            os.close(fd)

    def verify_integrity(self):
        for entry in self.policy.get('pinned_files', []):
            p = guard.no_links(Path(entry['path']))
            if not p.is_file() or sha(p) != entry['sha256']:
                raise ValueError('Protected hook/checker changed; review the exact new version before continuing')

    def ensure_task(self, repo, state):
        if state['task_started']:
            return
        folder = guard.common_of(repo) / 'codex-workflow'
        state['existing_task_ids'] = [p.stem for p in folder.glob('*.json')] if folder.exists() else []
        cfg, cfg_hash = guard.config_of(repo)
        for output in cfg['output_roots']:
            if guard.git(repo, 'ls-files', '-z', '--', output).stdout:
                raise ValueError('Output roots cannot conceal tracked source: ' + output)
        guard.begin_task(repo, state['task'], state['requirements'],
                         own_paths=['.codex/workflow.json'] if state.get('config_created_by_hook') else [])
        state['entry_task'] = state['task']
        state['adapting'] = False
        state['task_started'] = True
        state['baseline_captures'] += 1
        state['initial_dirty'] = changed_paths(repo)
        candidates = []
        for identifier in state['existing_task_ids']:
            try:
                original = guard.regular_json(folder / (identifier + '.json'))
                pending = original.get('last_observation', {})
                if (original.get('repo') == str(repo) and original.get('config_sha256') == cfg_hash
                        and (pending.get('unfinished') or pending.get('blocked'))):
                    candidates.append(identifier)
            except (ValueError, OSError, KeyError):
                continue
        state['resume_candidates'] = candidates
        if state.get('resume_requested') and len(candidates) == 1:
            self.bind_existing(repo, state, candidates[0], '用户明确续做；只有一份同仓库同配置、保留真实未完成对象的原任务记录')
            state['original_baseline_preserved'] = True
        elif (state.get('resume_requested') or state.get('turn_closeout')) and candidates:
            state['resume_binding_required'] = True
        if 'local-main' in state['requirements']:
            with guard.store(repo, state['task']) as (_, file):
                task = guard.regular_json(file)
            primary = Path(task['main_root'])
            if primary != repo:
                guard.add_worktree(repo, state['task'], primary)

    def extend(self, repo, state, tests=()):
        self.ensure_task(repo, state)
        with guard.store(repo, state['task']) as (_, path):
            task = guard.regular_json(path)
            task['requirements'] = sorted(set(task['requirements']) | set(state['requirements']))
            task['selected_tests'] = sorted(set(task.get('selected_tests', [])) | set(tests))
            guard.atomic(path, task)

    def bind_existing(self, repo, state, target, reason):
        if not reason.strip() or target not in state.get('existing_task_ids', []):
            raise ValueError('Bind the exact original task that existed before this hook baseline, with its concrete reason')
        with guard.store(repo, state.get('entry_task', state['task'])) as (_, path):
            auto = guard.regular_json(path)
        with guard.store(repo, target) as (_, path):
            original = guard.regular_json(path)
            original_sha = sha(path)
        if original['repo'] != str(repo) or original['config_sha256'] != guard.config_of(repo)[1]:
            raise ValueError('Original task repository/configuration does not match')
        observed = {x['object']: x.get('facts') for x in original.get('last_observation', {}).get('unfinished', [])}
        recognized, initial_count = [], 0
        original_trees = dict(original['baseline']['worktrees'], **original.get('additional_baselines', {}))
        for tree, snapshot in auto['baseline']['worktrees'].items():
            initial = snapshot.get('changes', {}); initial_count += len(initial)
            old = original_trees.get(tree, {}).get('changes', {})
            own = original.get('own_paths', {}).get(tree, [])
            for name, facts in initial.items():
                if name in own or observed.get('unsaved:' + tree + ':' + name) == facts:
                    recognized.append(tree + ':' + name)
                elif old.get(name) != facts:
                    raise ValueError('Original record does not account for existing change: ' + tree + ':' + name)
        if initial_count and not recognized:
            raise ValueError('An unrelated clean/empty task cannot hide existing unsaved work')
        previous = state['task']
        state.update(task=target, bound_existing=True, adoption_unattributed=[])
        state['resume_binding_required'] = False
        state.setdefault('task_links', []).append({'from': previous, 'to': target,
                                                  'original_sha256': original_sha, 'reason': reason})
        self.extend(repo, state)
        state['last_audit'] = None

    def protected(self, path):
        protected = [self.policy_path, Path(__file__).resolve(), Path(__file__).with_name('task_guard.py')]
        protected.extend(Path(x['path']) for x in self.policy.get('pinned_files', []))
        protected.extend(Path(p) for p in self.policy.get('protected_files', []))
        return any(path == p.absolute() for p in protected)

    def dispatch(self, event, repo, state):
        name = event['hook_event_name']
        if name == 'UserPromptSubmit':
            prompt = event.get('prompt', '')
            declared = closeout_requirements(prompt)
            state.update(turn=event.get('turn_id'), mutations=0, stop_retries=0,
                         turn_closeout=bool(declared),
                         resume_requested=bool(re.search(r'继续|接着|续做|之前|原任务|上次|返修', prompt)),
                         paused=bool(re.search(r'暂停|先停|停止执行|^stop\b', prompt, re.I)),
                         full_tests_requested=bool(re.search(r'运行全套|跑全套|完整测试', prompt) and not re.search(r'不能|不要|不跑|无需|不用', prompt)))
            state['requirements'] = sorted(set(state['requirements']) | declared)
            if state['task_started']:
                self.extend(repo, state)
                if declared and not state.get('source_ever_changed') and not state.get('bound_existing'):
                    with guard.store(repo, state['task']) as (_, file):
                        state['adoption_unattributed'] = list(guard.regular_json(file)['baseline']['worktrees'][str(repo)]['changes'])
            elif 'commit' in state['requirements'] and (repo / '.codex/workflow.json').is_file():
                self.ensure_task(repo, state)
                state['adoption_unattributed'] = changed_paths(repo)
            return context(name, '本仓库已接入自动轻检查。正常开发保留未提交草稿；只有实际请求的收口项才进入最终核对。不会自动启动测试或授予提交、发布、删除权限。当前会话标识：' + state['session_id'] + '；续做旧任务时须绑定准确原任务，不以新快照冒充旧成果已保存。')
        if name == 'Interrupt':
            state['paused'] = True
            return {}
        if name == 'PreToolUse':
            return self.pre(event, repo, state)
        if name == 'PostToolUse':
            pending = state['pending_tools'].pop(event.get('tool_use_id', ''), None)
            if pending:
                state['mutations'] += 1
                state['owned_paths'] = sorted(set(state['owned_paths']) | set(pending['paths']))
                state['last_audit'] = None
                if any(p not in state.get('initial_dirty', []) for p in changed_paths(repo)):
                    state['source_ever_changed'] = True
                    state['adoption_unattributed'] = []
                    if not state.get('resume_requested'):
                        state['resume_binding_required'] = False
                if pending.get('exit_marker'):
                    response = event.get('tool_response', '')
                    if not isinstance(response, str):
                        response = json.dumps(response)
                    matches = re.findall(re.escape(pending['exit_marker']) + r':([0-9]+)(?:\s|$)', response)
                    code = int(matches[-1]) if matches else None
                    failures = state.setdefault('command_failures', {})
                    if code == 0:
                        failures.pop(pending['command_key'], None)
                    else:
                        failures[pending['command_key']] = {'exit_code': code, 'tool_use_id': event.get('tool_use_id')}
            return {}
        if name == 'Stop':
            return self.stop(event, repo, state)
        return {}

    def pre(self, event, repo, state):
        tool = event.get('tool_name', '')
        data = event.get('tool_input', {})
        if not isinstance(data, dict):
            raise ValueError('Unsupported native tool input')
        command = data.get('command', '')
        is_patch = tool in ('apply_patch', 'Edit', 'Write')
        is_shell = tool == 'Bash'
        if tool in ('view_image', 'get_goal', 'update_plan', 'request_user_input', 'request_user_input_async') or tool.startswith('mcp__openaiDeveloperDocs__'):
            return {}
        if not (is_patch or is_shell):
            # Unknown tools may have side effects; establish the baseline rather
            # than pretending all MCP/REPL operations are read-only.
            self.ensure_task(repo, state)
            state['pending_tools'][event.get('tool_use_id', '')] = {'paths': []}
            return context('PreToolUse', '该工具的写入范围不能自动确定，已记录基线；结束时仍核对实际项目变化。')
        if not isinstance(command, str):
            raise ValueError('Native command missing')
        args = tokens(command)
        if is_shell:
            readonly = readonly_command(command)
            if readonly is not None:
                if readonly == command:
                    return {}
                return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                              'updatedInput': dict(data, command=readonly)}}
        manager_path = Path(__file__).with_name('manage_task_hooks.py').resolve()
        alias = self.policy.get('management_entry', {})
        manager_paths = {str(manager_path), alias.get('path')}
        if is_shell and len(args) >= 3 and Path(shutil.which(args[0]) or '/nonexistent-python').resolve() == Path(sys.executable).resolve() and args[1] == '-B' and args[2] in manager_paths and segments(command) and len(segments(command)) == 1:
            if args[2] == alias.get('path') and (not Path(args[2]).is_file()
                    or sha(guard.no_links(Path(args[2]))) != alias.get('sha256')):
                return deny('共用管理命令版本已变化；请使用安装回执中的固定管理入口。普通检查仍使用已验收版本。')
            # Control operations execute synchronously, like init/bind. Removing
            # PostToolUse must not strand the control command in pending_tools.
            cli = argparse.ArgumentParser(add_help=False)
            cli.add_argument('--home', default=str(self.policy_path.parent.parent.parent))
            cli.add_argument('action', choices=['status', 'disable', 'enable', 'uninstall', 'recover'])
            cli.add_argument('--apply', action='store_true')
            try:
                control = cli.parse_args(args[3:])
            except SystemExit:
                return deny('管理入口参数无效；未停用检查。')
            result = management.manage(control.home, control.action, control.apply)
            output = shlex.join(['printf', '%s\n', json.dumps(result, ensure_ascii=False)])
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                          'updatedInput': dict(data, command=output)}}
        cfg_path = repo / '.codex/workflow.json'
        if not cfg_path.exists() and is_shell and str(Path(__file__).with_name('task_guard.py')) in args and 'init' in args:
            if re.search(r'[\n;|&><`]|\$\(', command):
                return deny('首次接入请使用独立的准确 init 命令，不执行复合脚本。')
            def option(key):
                return args[args.index(key) + 1]
            if Path(option('--repo')).resolve() != repo:
                return deny('初始化目标必须是当前准确仓库。')
            setup = argparse.ArgumentParser(add_help=False)
            setup.add_argument('--main-branch'); setup.add_argument('--remote', default='origin')
            setup.add_argument('--output-root', action='append', required=True)
            try:
                settings = setup.parse_args(args[args.index('init') + 1:])
            except SystemExit:
                return deny('先核实项目的输出目录，再用 init --output-root 精确接入。')
            for output in settings.output_root:
                guard.relative(output)
                if guard.git(repo, 'ls-files', '-z', '--', output).stdout:
                    return deny('输出目录不能包含受 Git 跟踪的源码。')
            created = guard.init_project(repo, settings.main_branch, settings.remote, settings.output_root)
            state.update(adapting=True, config_created_by_hook=created['state'] == 'initialized')
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                          'updatedInput': dict(data, command="printf '项目适配配置已准备；请补齐相关测试后开始开发\\n'")}}
        if state.get('adapting') and is_patch and paths_if_config(command, repo):
            return {}
        if not cfg_path.is_file():
            if is_shell and is_readonly(command):
                return {}
            return deny('此项目尚未接入。先只读核实输出目录与测试命令，使用共用 task_guard.py --repo 当前仓库 init --output-root 精确目录；不能跳过后直接修改。')
        if is_shell and str(Path(__file__).resolve()) in args and '--bind-task' in args:
            def option(key):
                return args[args.index(key) + 1]
            if option('--session') != state['session_id'] or Path(option('--repo')).resolve() != repo:
                return deny('绑定只能用于当前准确的聊天与项目，不能另选干净目录。')
            self.bind_existing(repo, state, option('--bind-task'), option('--reason'))
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                          'updatedInput': dict(data, command="printf '已沿用原任务记录；未重置基线\\n'")}}
        if is_shell and is_readonly(command):
            return {}
        paths = patch_paths(command, event.get('cwd', str(repo))) if is_patch else []
        for path in paths:
            guard.no_links(path)
            if self.protected(path):
                return deny('检查配置与程序受保护；本次开发不能通过编辑它们跳过检查。更新检查器须独立审阅并重新信任。')
            if not path.is_relative_to(repo):
                return deny('写入另一个目录前须明确登记本任务范围；其他任务成果保持原样。')
        if is_shell:
            args = tokens(command)
            if any(str(p) in command for p in [self.policy_path, Path(__file__).resolve(), *map(Path, self.policy.get('protected_files', []))]):
                return deny('命令涉及受保护的自动检查配置；本任务不能停用或改写它们。')
        self.ensure_task(repo, state)
        if state.get('resume_binding_required'):
            return deny('存在未完成原任务，不能用新基线跳过。先用 --bind-task 绑定准确记录：' + ', '.join(state['resume_candidates']))
        with guard.store(repo, state['task']) as (_, file):
            task = guard.regular_json(file)
        original = task['baseline']['worktrees'][str(repo)]['changes']
        allowed_original = task.get('own_paths', {}).get(str(repo), [])
        if is_shell:
            for relative in original:
                if relative not in allowed_original and relative not in state['owned_paths'] and relative in command:
                    return deny('命令涉及开工前已有成果，不能在归属未核对时修改：' + relative)
        for path in paths:
            relative = path.relative_to(repo).as_posix()
            if relative in original and relative not in allowed_original and relative not in state['owned_paths']:
                return deny('该文件开工前已有待保护成果，不能直接覆盖：' + relative)
        state['pending_tools'][event.get('tool_use_id', '')] = {'paths': [p.relative_to(repo).as_posix() for p in paths]}
        cfg = guard.regular_json(repo / '.codex/workflow.json')
        args = tokens(command) if is_shell else []
        selected = next((k for k, v in cfg.get('tests', {}).items() if args == v), None)
        script = args[1] if len(args) > 1 and args[0] in ('pnpm', 'npm', 'yarn') else ''
        observed = [p for p in changed_paths(repo) if p not in original or p in allowed_original]
        docs_only = bool(observed) and all(low_impact(p) for p in observed)
        if docs_only and (script in BROAD_SCRIPTS or selected in self.policy.get('heavy_tests', [])) and not state.get('full_tests_requested'):
            state['pending_tools'].pop(event.get('tool_use_id', ''), None)
            return deny('本轮仅有文案/样式改动，拒绝启动无关整套测试；请运行受影响检查。')
        if selected:
            state['requirements'] = sorted(set(state['requirements']) | {'tests'})
            self.extend(repo, state, [selected])
            rewritten = shlex.join([sys.executable, '-B', str(Path(__file__).with_name('task_guard.py')),
                                    '--repo', str(repo), 'run', '--task', state['task'], '--test', selected])
            command_to_run = rewritten
        else:
            command_to_run = command
        if is_shell:
            # Codex's native PostToolUse Bash payload contains output without a
            # structured exit code. A per-call marker records the actual child
            # result; missing/truncated output remains unknown, never green.
            marker = '__CODEX_TASK_EXIT_' + uuid.uuid4().hex + '__'
            state['pending_tools'][event.get('tool_use_id', '')].update(
                exit_marker=marker, command_key=guard.digest(command))
            wrapped = '(\n' + command_to_run + '\n)\ncodex_task_exit=$?\nprintf "\\n' + marker + ':%s\\n" "$codex_task_exit"\nexit "$codex_task_exit"'
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                          'updatedInput': dict(data, command=wrapped)}}
        return context('PreToolUse', '已自动记录本任务基线；轻检查通过。未运行任何测试。')

    def stop(self, event, repo, state):
        if not state['task_started'] or state.get('paused') or (not state['mutations'] and not state.get('turn_closeout') and not state['pending_tools']):
            return {}
        self.extend(repo, state)
        result = guard.check_task(repo, state['task'])
        state['full_audits'] += 1
        all_pending = list(result['unfinished']) + list(result.get('blocked', []))
        deferred_prefixes = ('unsaved:', 'remaining-branch:', 'merge', 'push', 'local-main', 'deploy-record')
        deferred = [x for x in all_pending if not state.get('turn_closeout') and x['object'].startswith(deferred_prefixes)]
        failed = [x for x in all_pending if x not in deferred]
        if state.get('resume_binding_required'):
            failed.append({'object': 'existing-task-binding-required', 'version': guard.digest(state['resume_candidates'])})
        failed.extend({'object': 'command:' + key, 'version': guard.digest(facts)}
                      for key, facts in state.get('command_failures', {}).items())
        if state['pending_tools']:
            failed.append({'object': 'native-tools-not-finished', 'version': guard.digest(state['pending_tools'])})
        current = guard.regular_json(guard.common_of(repo) / 'codex-workflow' / (state['task'] + '.json'))
        snapshot = current['last_observation']
        changed = changed_paths(repo)
        owned = [p for p in changed if p not in current['baseline']['worktrees'][str(repo)]['changes']]
        if owned:
            state['source_ever_changed'] = True
        if not state.get('turn_closeout') and owned and all(low_impact(p) for p in owned):
            old_tests = [x for x in failed if x['object'].startswith('test:')]
            deferred.extend(old_tests)
            failed = [x for x in failed if x not in old_tests]
        if any(not low_impact(p) for p in owned) and not current.get('selected_tests'):
            failed.append({'object': 'focused-tests-required', 'version': guard.digest([snapshot['candidate'], owned]),
                           'next_action': '运行与本次程序改动有关的配置测试；不要求整套测试。'})
        if state.get('adoption_unattributed') and state.get('turn_closeout'):
            failed.append({'object': 'existing-task-binding-required',
                           'version': guard.digest(state['adoption_unattributed']),
                           'next_action': '首次接入前已有修改，须绑定原任务记录核对归属；不能新拍基线冒充成果已保存。'})
        whitespace = execute('git', '--no-optional-locks', 'diff', '--check', '--', *owned, cwd=repo) if owned else None
        if whitespace and whitespace.returncode:
            failed.append({'object': 'diff-check', 'version': guard.digest(whitespace.stdout.hex())})
        state['last_audit'] = {'candidate': result['candidate'], 'unfinished': [x['object'] for x in failed],
                               'machine_checks_complete': not failed, 'tests_auto_started': 0,
                               'deferred_closeout': [x['object'] for x in deferred],
                               'phase': 'closeout' if state.get('turn_closeout') else 'draft'}
        if not failed:
            state['stop_retries'] = 0
            return {}
        state['stop_retries'] = state.get('stop_retries', 0) + 1
        summary = '；'.join(x['object'] for x in failed[:8])
        if result['state'] == 'blocked' or state['stop_retries'] > 2:
            return {'continue': False, 'stopReason': '自动核对尚未通过，保留部分完成状态',
                    'systemMessage': '任务尚未收口：' + summary + '。真实受阻时保持现场，不扩大提交/删除权限。'}
        return {'decision': 'block', 'reason': '自动核对发现未完成项：' + summary +
                '。在现有授权内处理并重查；无法安全处理则报告准确阻碍与部分完成，不能用无关成功或改规则消除。不要运行无关全套测试。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--policy-sha256')
    parser.add_argument('--bind-task')
    parser.add_argument('--repo')
    parser.add_argument('--session')
    parser.add_argument('--reason')
    args = parser.parse_args(argv)
    event = {}
    try:
        if args.bind_task:
            if not all([args.repo, args.session, args.reason]):
                raise ValueError('Binding requires repo, session, exact original task and reason')
            event = {'session_id': args.session, 'cwd': args.repo, 'hook_event_name': 'PreToolUse',
                     'tool_name': 'Bash', 'tool_input': {'command': shlex.join([
                         sys.executable, str(Path(__file__).resolve()), '--bind-task', args.bind_task,
                         '--repo', args.repo, '--session', args.session, '--reason', args.reason])}}
        else:
            payload = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(payload) > MAX_INPUT:
                raise ValueError('Native event exceeds input limit')
            event = json.loads(payload)
        if args.policy_sha256 and sha(args.policy) != args.policy_sha256:
            raise ValueError('Hook policy changed; trust the exact updated definition before continuing')
        result = Hooks(args.policy, args.state_root).handle(event)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        message = '自动检查执行失败，不能当作通过：' + type(error).__name__ + ': ' + str(error)
        if event.get('hook_event_name') == 'PreToolUse':
            print(json.dumps(deny(message), ensure_ascii=False))
            return 0
        if event.get('hook_event_name') == 'Stop':
            print(json.dumps({'continue': False, 'stopReason': message, 'systemMessage': message}, ensure_ascii=False))
            return 0
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
