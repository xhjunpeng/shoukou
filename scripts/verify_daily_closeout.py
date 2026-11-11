#!/usr/bin/env python3
"""Read-only final checks for one explicitly selected daily closeout task.

Only declared paths and refs are checked. Scope completeness, user authority and
original evidence truth are not authenticated. No cleanup permission is granted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

sys.dont_write_bytecode = True
from closeout_progress import assess, evidence_valid

REQUIREMENTS = {'merge', 'local-main', 'sources', 'restart-dependencies',
                'tests', 'push', 'deploy', 'cleanup'}
EXIT_CODES = {'complete': 0, 'continue': 3, 'blocked': 1}
BOUNDARY = ('Only explicitly declared task requirements, paths and refs were checked. '
            'The caller must derive requirements from the actual user request. '
            'Omitted sources, scope completeness, authority and original evidence truth '
            'are not authenticated. Local refs alone do not prove a live remote result. '
            'Restart checks inspect declared script text for literal paths only; they '
            'do not execute scripts or certify all runtime dependencies. '
            'No mutation or cleanup permission is granted.')


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('Invalid or missing fields: ' + ', '.join(required))
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and all(ord(c) >= 32 for c in value)


def _absolute(value):
    if not _text(value) or not Path(value).is_absolute():
        raise ValueError('Declared filesystem paths must be absolute')
    return Path(value)


def _relative(value):
    if (not _text(value) or '\\' in value or PurePosixPath(value).is_absolute()
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(char in value for char in '*?[]')):
        raise ValueError('Source repo_path must be a literal relative file path without traversal')
    return value


def _git(repo, *args, check=True):
    result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                             '-C', str(repo), *args], capture_output=True, timeout=30,
                            env=dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0'))
    if check and result.returncode:
        raise ValueError('Git query failed: ' + args[0])
    return result


def _value(repo, *args):
    return os.fsdecode(_git(repo, *args).stdout).strip()


def _revision(repo, ref):
    result = _git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}', check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def _common(repo):
    path = Path(_value(repo, 'rev-parse', '--git-common-dir'))
    return (repo / path).resolve() if not path.is_absolute() else path.resolve()


def _registered(repo, path, branch):
    records = _git(repo, 'worktree', 'list', '--porcelain', '-z').stdout.split(b'\0\0')
    for record in records:
        fields = dict(os.fsdecode(line).partition(' ')[::2]
                      for line in record.split(b'\0') if b' ' in line)
        if (fields.get('worktree') and Path(fields['worktree']).resolve() == path.resolve()
                and fields.get('branch') == 'refs/heads/' + branch):
            return True
    return False


def _file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Declared file is missing, non-regular or a symlink')
    data = path.read_bytes()
    return data, '100755' if path.stat().st_mode & 0o111 else '100644'


def _validate(plan):
    _object(plan, {'schema_version', 'task', 'repo', 'saved_commit', 'target_ref',
                   'requirements', 'local_main', 'sources', 'restart_dependencies',
                   'remote_target', 'cleanup_has_services', 'evidence', 'stops'},
            ('schema_version', 'task', 'repo', 'saved_commit', 'target_ref', 'requirements'))
    if plan['schema_version'] != 1 or not _text(plan['task']):
        raise ValueError('Expected schema_version 1 and a named task')
    if not isinstance(plan['saved_commit'], str) or not re.fullmatch(r'[0-9a-f]{7,64}', plan['saved_commit']):
        raise ValueError('saved_commit must identify a committed candidate by hexadecimal object ID')
    if not _text(plan['target_ref']) or plan['target_ref'].startswith('-'):
        raise ValueError('Invalid target_ref')
    requirements = plan['requirements']
    if (not isinstance(requirements, list) or not requirements
            or any(not isinstance(item, str) or item not in REQUIREMENTS for item in requirements)
            or len(set(requirements)) != len(requirements)):
        raise ValueError('requirements must name unique supported requirements from the user request')
    _absolute(plan['repo'])
    if 'local_main' in plan:
        item = _object(plan['local_main'], {'path', 'branch'}, ('path', 'branch'))
        _absolute(item['path'])
        if not _text(item['branch']) or item['branch'].startswith('-'):
            raise ValueError('Invalid local_main branch')
    for name, fields in (('sources', ('path', 'repo_path')),
                         ('restart_dependencies', ('script', 'paths'))):
        if not isinstance(plan.get(name, []), list):
            raise ValueError(name + ' must be a list')
        for item in plan.get(name, []):
            _object(item, fields, fields)
            _absolute(item[fields[0]])
            if name == 'sources':
                _relative(item['repo_path'])
            elif not isinstance(item['paths'], list) or not item['paths']:
                raise ValueError('Restart dependencies need explicit deletion paths')
            else:
                for path in item['paths']:
                    _absolute(path)
    if 'remote_target' in plan:
        remote = _object(plan['remote_target'], {'remote', 'ref'}, ('remote', 'ref'))
        if (not isinstance(remote['remote'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', remote['remote'])
                or not _text(remote['ref']) or not remote['ref'].startswith('refs/heads/')):
            raise ValueError('remote_target must name a configured remote and a full branch ref')
    if 'cleanup_has_services' in plan and type(plan['cleanup_has_services']) is not bool:
        raise ValueError('cleanup_has_services must be a boolean declaration')
    evidence = _object(plan.get('evidence', {}), {'tests', 'push', 'deploy', 'cleanup'})
    for item in evidence.values():
        _object(item, {'path', 'sha256'}, ('path', 'sha256'))
        _absolute(item['path'])
        if not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('Evidence requires a SHA256 digest')


def verify(plan):
    """Check actual declared objects, then delegate unfinished-state decisions to assess."""
    _validate(plan)
    repo = _absolute(plan['repo']).resolve()
    if Path(_value(repo, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise ValueError('repo must be the exact worktree root')
    saved = _revision(repo, plan['saved_commit'])
    target = _revision(repo, plan['target_ref'])
    initial_head = _revision(repo, 'HEAD')
    requirements = set(plan['requirements'])
    if 'push' in requirements:
        requirements.add('merge')
    if 'deploy' in requirements:
        requirements.add('sources')
    if 'cleanup' in requirements and plan.get('cleanup_has_services') is True:
        requirements.add('restart-dependencies')
    checks, pending = [], []

    def record(name, passed, facts):
        version = hashlib.sha256(json.dumps([saved, facts], sort_keys=True).encode()).hexdigest()
        checks.append({'object': name, 'version': version, 'passed': passed, 'facts': facts})
        if not passed:
            pending.append({'object': name, 'version': version,
                            'next_action': 'Resolve the observed mismatch or attach an object-specific supported stopping condition'})

    record('saved-commit', saved is not None, {'requested': plan['saved_commit'], 'actual': saved})
    if requirements & {'merge', 'local-main', 'push'}:
        record('target-ref', target is not None, {'ref': plan['target_ref'], 'actual': target})
    if 'merge' in requirements:
        result = _git(repo, 'merge-base', '--is-ancestor', saved, target, check=False) if saved and target else None
        record('merge', result is not None and result.returncode == 0,
               {'saved_commit': saved, 'target_commit': target,
                'query_exit': result.returncode if result is not None else None})
    local, initial_local = plan.get('local_main'), None
    if 'local-main' in requirements:
        facts = {'declared': local, 'target_commit': target}
        if local:
            path = _absolute(local['path'])
            try:
                root = Path(_value(path, 'rev-parse', '--show-toplevel')).resolve()
                branch = _value(path, 'branch', '--show-current')
                initial_local = (path, _revision(path, 'HEAD'), branch)
                facts.update(head=initial_local[1], branch=branch,
                             exact_root=root == path.resolve(), same_repository=_common(path) == _common(repo),
                             registered=_registered(repo, path, local['branch']))
            except (ValueError, OSError):
                facts['query_failed'] = True
        ok = (target is not None and initial_local is not None and initial_local[1] == target
              and facts.get('branch') == local['branch'] and facts.get('exact_root')
              and facts.get('same_repository') and facts.get('registered'))
        record('local-main', bool(ok), facts)
    if 'sources' in requirements:
        if not plan.get('sources'):
            record('sources', False, {'reason': 'No exact release-input source files declared'})
        for source in plan.get('sources', []):
            facts = dict(source)
            try:
                data, mode = _file(_absolute(source['path']))
                facts.update(actual_sha256=hashlib.sha256(data).hexdigest(), actual_mode=mode)
                tree = _git(repo, 'ls-tree', '-z', saved, '--', source['repo_path'], check=False) if saved else None
                entries = [entry for entry in tree.stdout.split(b'\0') if entry] if tree else []
                exact = [entry for entry in entries if entry.split(b'\t', 1)[1] == os.fsencode(source['repo_path'])]
                if len(exact) != 1:
                    raise ValueError('No exact saved source blob')
                saved_mode, kind, oid = exact[0].split(b'\t', 1)[0].decode().split()
                if kind != 'blob' or saved_mode not in ('100644', '100755'):
                    raise ValueError('Saved source must be a regular file')
                expected = _git(repo, 'cat-file', 'blob', oid).stdout
                facts.update(saved_sha256=hashlib.sha256(expected).hexdigest(), saved_mode=saved_mode)
                ok = data == expected and mode == saved_mode
            except (ValueError, OSError) as error:
                facts['reason'] = str(error)
                ok = False
            record('source:' + source['repo_path'], ok, facts)
    if 'restart-dependencies' in requirements:
        if not plan.get('restart_dependencies'):
            record('restart-dependencies', False, {'reason': 'No restart scripts and deletion paths declared'})
        for item in plan.get('restart_dependencies', []):
            facts = dict(item)
            try:
                data, _ = _file(_absolute(item['script']))
                text = data.decode('utf-8')
                references = []
                for path in item['paths']:
                    variants = {path.rstrip('/'), str(Path(path).resolve()).rstrip('/')}
                    if any(re.search(r'(?<![\w/.-])' + re.escape(variant) + r'(?![\w.-])', text)
                           for variant in variants):
                        references.append(path)
                facts.update(script_sha256=hashlib.sha256(data).hexdigest(), references=references)
                ok = not references
            except (ValueError, OSError, UnicodeDecodeError) as error:
                facts['reason'] = str(error)
                ok = False
            record('restart:' + item['script'], ok, facts)
    if 'cleanup' in requirements:
        record('cleanup:service-scope', 'cleanup_has_services' in plan,
               {'cleanup_has_services': plan.get('cleanup_has_services')})
    for name in sorted(requirements & {'tests', 'push', 'deploy', 'cleanup'}):
        evidence = plan.get('evidence', {}).get(name)
        record('evidence:' + name, evidence_valid(evidence), {'evidence': evidence})
    if 'push' in requirements:
        remote = plan.get('remote_target')
        facts = {'remote_target': remote, 'target_commit': target}
        if remote:
            result = _git(repo, 'ls-remote', '--exit-code', '--refs', remote['remote'], remote['ref'], check=False)
            if result.returncode not in (0, 2):
                raise ValueError('Git remote query failed: remote=' + remote['remote']
                                 + ' ref=' + remote['ref'] + ' exit=' + str(result.returncode))
            heads = [line.split() for line in result.stdout.decode().splitlines()]
            matches = [parts[0] for parts in heads if len(parts) == 2 and parts[1] == remote['ref']]
            facts.update(query_exit=result.returncode, remote_commits=matches)
            ok = result.returncode == 0 and target is not None and matches == [target]
        else:
            ok = False
        record('push:remote-target', ok, facts)
    final = {'repo_head': _revision(repo, 'HEAD'), 'target': _revision(repo, plan['target_ref'])}
    before = {'repo_head': initial_head, 'target': target}
    if initial_local:
        path, head, branch = initial_local
        before['local_main'] = [head, branch]
        final['local_main'] = [_revision(path, 'HEAD'), _value(path, 'branch', '--show-current')]
    record('git-state-stable', before == final, {'before': before, 'after': final})
    execution = assess(pending, plan.get('stops'))
    return {'schema_version': 1, 'mode': 'daily-final', 'task': plan['task'],
            'saved_commit': saved, 'target_ref': plan['target_ref'], 'target_commit': target,
            'requirements_checked': sorted(requirements), 'checks': checks,
            **execution, 'boundary': BOUNDARY}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Exact task plan JSON; no scripts are executed')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = verify(json.loads(Path(args.input).read_text()))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result['state'])
            for item in result['unfinished'] + result['blocked']:
                print(item['object'])
            print(BOUNDARY)
        return EXIT_CODES[result['state']]
    except (ValueError, OSError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'state': 'input-error', 'error': str(error), 'boundary': BOUNDARY}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
