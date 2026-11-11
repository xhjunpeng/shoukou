#!/usr/bin/env python3
"""Separate unfinished closeout work from evidence-backed stopping conditions.

Evidence integrity is checked; its truth, scope and authority are not authenticated.
This module never authorizes a mutation or weakens a cleanup preflight.
"""
import hashlib
from pathlib import Path


STOP_KINDS = {'user-pause', 'app-protection', 'query-error', 'scope-boundary',
              'external-dependency', 'acceptance-failure'}


class IncompleteReview(ValueError):
    def __init__(self, message, items):
        super().__init__(message)
        self.items = items


def text(value):
    return isinstance(value, str) and bool(value.strip())


def evidence_valid(evidence):
    if not isinstance(evidence, dict) or not text(evidence.get('path')):
        return False
    path = Path(evidence['path'])
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            return False
        with path.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        return actual == evidence.get('sha256')
    except OSError:
        return False


def assess(items, stops=None):
    """A stop for one exact object/version cannot excuse other unfinished work."""
    if stops is None:
        stops = {}
    if not isinstance(stops, dict):
        raise ValueError('Stopping conditions must be an object map')
    objects = {item['object'] for item in items}
    if len(objects) != len(items) or set(stops) - objects:
        raise ValueError('Stopping conditions must name unique current unfinished objects')
    unfinished, blocked = [], []
    for item in items:
        stop = stops.get(item['object'])
        if not isinstance(stop, dict):
            unfinished.append(dict(item, reason='investigation-or-processing-not-finished'))
            continue
        fields = ('attempt', 'result', 'release_condition')
        if (not isinstance(stop.get('kind'), str) or stop['kind'] not in STOP_KINDS
                or stop.get('version') != item['version']
                or any(not text(stop.get(field)) for field in fields)
                or not evidence_valid(stop.get('evidence'))):
            unfinished.append(dict(item, reason='stopping-condition-evidence-invalid'))
            continue
        blocked.append(dict(item, stop=stop))
    state = 'continue' if unfinished else ('blocked' if blocked else 'complete')
    return {'state': state, 'unfinished': unfinished, 'blocked': blocked,
            'boundary': 'Evidence bytes and object/version bindings only; the caller must read the original records and verify actual authority and relevance. No cleanup permission granted.'}


def from_checks(pending, failures, version, stops=None, versions=None):
    versions = versions or {}
    items = [{'object': key, 'version': versions.get(key, version),
              'next_action': 'Resolve the failed check and rerun verification' if key in failures
              else 'Complete the object-specific investigation or processing in the existing scope'}
             for key in sorted(set(pending) | set(failures))]
    return assess(items, stops)
