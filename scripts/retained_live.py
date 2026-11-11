"""Narrow read-only observations for retained live logs and JSON caches.

This accepts sampled metadata changes, not content-integrity or backup claims.
Owner/source relevance remains a caller judgment; no mutation is authorized.
"""
import hashlib
import json
from pathlib import Path
import stat
import time

import inspect_repo as git_state
import closeout_progress as progress


def check(path, name, decision, before, after, started):
    dynamic = decision.get('dynamic')
    if not isinstance(dynamic, dict):
        return False, 'missing-declaration'
    if (name.endswith('/') or decision.get('action') != 'retained'
            or decision.get('retention') not in ('agreed', 'required')
            or dynamic.get('path') != str(path)):
        return False, 'exact-retained-file-required'
    role = dynamic.get('role')
    if not ((role == 'append-log' and decision.get('kind') == 'runtime' and path.suffix == '.log')
            or (role == 'replace-cache' and decision.get('kind') == 'cache'
                and path.suffix == '.json' and progress.text(decision.get('rebuild')))):
        return False, 'unsupported-kind-or-role'
    observed = dynamic.get('observed_at_unix')
    if type(observed) not in (int, float) or not -5 <= started - observed <= 120:
        return False, 'owner-observation-not-fresh-at-start'
    source = dynamic.get('source')
    if (not progress.evidence_valid(source) or Path(source['path']).resolve() == path.resolve()):
        return False, 'owner-source-invalid'
    if (not before or not after or not stat.S_ISREG(before[0]) or not stat.S_ISREG(after[0])
            or before[0] != after[0] or path.is_symlink() or not path.is_file()
            or any(p.name == '.git' for p in [path, *path.parents])):
        return False, 'file-missing-or-type-permissions-changed'
    root = git_state.run(['git', '-C', str(path.parent), 'rev-parse', '--show-toplevel'])
    if root.returncode == 0:
        top = Path(root.stdout.decode().strip())
        tracked = git_state.run(['git', '-C', str(top), 'ls-files', '--error-unmatch', '--', str(path.relative_to(top))])
        if tracked.returncode == 0:
            return False, 'tracked-source-cannot-be-dynamic'
    pid = dynamic.get('pid')
    if type(pid) is not int or not 0 < pid <= 2147483647:
        return False, 'owner-pid-invalid'
    owner = git_state.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='])
    if (owner.returncode or not owner.stdout.strip()
            or hashlib.sha256(owner.stdout.strip()).hexdigest() != dynamic.get('owner_signature')):
        return False, 'owner-missing-or-replaced'
    if role == 'append-log':
        if after[1] < before[1] or (after[1] == before[1] and after[2] != before[2]):
            return False, 'log-truncated-or-rewritten'
        if len(before) >= 5 and len(after) >= 5 and before[3:5] != after[3:5]:
            return False, 'log-identity-changed'
    else:
        try:
            json.loads(path.read_bytes())
        except (OSError, ValueError):
            return False, 'cache-not-valid-json'
    return True, None


def observation(path, decision, before, after):
    return {'path': str(path), 'role': decision['dynamic']['role'], 'pid': decision['dynamic']['pid'],
            'observed_at_unix': time.time(), 'bytes_before': before[1], 'bytes_after': after[1],
            'measurement': 'Live interval observation; no static snapshot or unchanged-content claim'}


def directory_metadata_only(before, after):
    return (before and after and stat.S_ISDIR(before[0]) and before[0] == after[0]
            and before[3:5] == after[3:5])
