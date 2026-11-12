"""Recognize bounded read-only queries; never authorize arbitrary Python."""
import ast
import builtins
import copy
from collections import Counter
import re
import shlex
import shutil
import sys
from pathlib import Path

READ = {'pwd', 'ls', 'cat', 'rg', 'head', 'tail', 'wc', 'which', 'true', 'printf'}
GIT = {'status', 'diff', 'log', 'show', 'ls-files', 'ls-tree', 'rev-parse', 'show-ref', 'for-each-ref', 'merge-base'}
CALLS = {'Path', 'print', 'str', 'len', 'list', 'dict', 'all', 'any', 'sorted', 'tuple', 'set', 'int', 'bool', 'isinstance'}
ATTRS = {'read_text', 'read_bytes', 'exists', 'is_file', 'is_dir', 'is_symlink', 'glob', 'rglob', 'iterdir', 'stat', 'st_size', 'get', 'items', 'values', 'keys', 'loads', 'dumps', 'sha256', 'hexdigest', 'join', 'name', 'parent', 'stem', 'suffix', 'parts'}


def query_python(command):
    """Return an isolated command for the small data-query subset, or None."""
    match = re.fullmatch(r"([^\n]+?)\s+<<\s*['\"]?([A-Za-z_][A-Za-z_0-9]*)['\"]?\n(.*)\n\2\s*", command, re.S)
    try:
        args = shlex.split(match[1] if match else command)
    except ValueError:
        return None
    if not args or not re.fullmatch(r'python3(?:\.[0-9]+)?', args[0].rsplit('/', 1)[-1]):
        return None
    interpreter = shutil.which(args[0])
    if not interpreter or Path(interpreter).resolve() != Path(sys.executable).resolve():
        return None
    if not match and (segments(command) is None or len(segments(command)) != 1):
        return None
    if match and not re.search(r"<<\s*[\'\"]", match[1] + " <<" + command.split("<<", 1)[1].split("\n", 1)[0]):
        return None
    options = args[1:]
    if match:
        if not options or options[-1] != '-':
            return None
        flags, source = options[:-1], match[3]
    else:
        if '-c' not in options or options.index('-c') != len(options) - 2:
            return None
        flags, source = options[:-2], options[-1]
    if any(x not in {'-B', '-I'} for x in flags) or len(source) > 16384:
        return None
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    blocked = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.With, ast.AsyncWith,
               ast.Try, ast.Raise, ast.Delete, ast.Global, ast.Nonlocal, ast.While, ast.Await, ast.Yield)
    for node in ast.walk(tree):
        if isinstance(node, blocked):
            return None
        if isinstance(node, ast.Import) and any(x.name not in {'json', 'hashlib'} or x.asname for x in node.names):
            return None
        if isinstance(node, ast.ImportFrom) and not (node.module == 'pathlib' and node.level == 0
                                                     and all(x.name == 'Path' and not x.asname for x in node.names)):
            return None
        if isinstance(node, ast.Attribute) and node.attr not in ATTRS:
            return None
        if isinstance(node, ast.Name) and (node.id.startswith('__') or (node.id in vars(builtins) and node.id not in CALLS)):
            return None
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id not in CALLS:
                return None
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                return None
            if any(k.arg is None or k.arg == 'file' for k in node.keywords):
                return None
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(not isinstance(t, (ast.Name, ast.Tuple, ast.List)) for t in targets):
                return None
    # -I prevents local json.py / PYTHONPATH from turning a read into execution.
    return shlex.join([args[0], '-I', '-B', '-c', source])


def diagnostic_correction(original, corrected):
    """Admit only additional JSON-object guards; preserve all query work.

    Both commands must be in the isolated read-only subset. This first version
    deliberately does not decide whether arbitrary rewritten code is equivalent.
    """
    before, after = query_python(original), query_python(corrected)
    if before is None or after is None:
        raise ValueError('Both diagnostic commands must be bounded read-only Python queries')

    def strip_guards(tree):
        guards = Counter()
        sites = []
        def visit(node, path=()):
            for field, value in ast.iter_fields(node):
                if isinstance(value, list):
                    kept = []; previous = None
                    for child in value:
                        variable = None
                        if (isinstance(node, ast.For) and field == 'body'
                                and isinstance(child, ast.If) and not child.orelse
                                and len(child.body) == 1 and isinstance(child.body[0], ast.Continue)
                                and isinstance(child.test, ast.UnaryOp) and isinstance(child.test.op, ast.Not)):
                            call = child.test.operand
                            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                                    and call.func.id == 'isinstance' and not call.keywords and len(call.args) == 2
                                    and isinstance(call.args[0], ast.Name) and isinstance(call.args[1], ast.Name)
                                    and call.args[1].id == 'dict'):
                                variable = call.args[0].id
                        if (variable and isinstance(previous, ast.Assign) and len(previous.targets) == 1
                                and isinstance(previous.targets[0], ast.Name) and previous.targets[0].id == variable
                                and isinstance(previous.value, ast.Call) and isinstance(previous.value.func, ast.Attribute)
                                and isinstance(previous.value.func.value, ast.Name)
                                and previous.value.func.value.id == 'json' and previous.value.func.attr == 'loads'
                                and len(previous.value.args) == 1 and not previous.value.keywords
                                and isinstance(previous.value.args[0], ast.Call)
                                and isinstance(previous.value.args[0].func, ast.Attribute)
                                and previous.value.args[0].func.attr == 'read_text'
                                and isinstance(previous.value.args[0].func.value, ast.Name)
                                and isinstance(node.target, ast.Name)
                                and node.target.id == previous.value.args[0].func.value.id):
                            guards[(path, len(kept), variable)] += 1
                            sites.append({'key': (path, len(kept), variable), 'line': child.lineno,
                                          'record': variable, 'input': node.target.id})
                            continue
                        if isinstance(child, ast.AST):
                            visit(child, path + (field, len(kept)))
                        kept.append(child); previous = child
                    setattr(node, field, kept)
                elif isinstance(value, ast.AST):
                    visit(value, path + (field,))
            return node
        return ast.dump(visit(copy.deepcopy(tree)), include_attributes=False), guards, sites

    original_tree = ast.parse(shlex.split(before)[-1])
    corrected_tree = ast.parse(shlex.split(after)[-1])
    if any(isinstance(node, ast.Assert) for tree in [original_tree, corrected_tree] for node in ast.walk(tree)):
        raise ValueError('Assertion-bearing verification is not a reviewable diagnostic query')
    left, old_guards, _ = strip_guards(original_tree)
    right, new_guards, sites = strip_guards(corrected_tree)
    if (left != right or old_guards - new_guards or not new_guards - old_guards):
        raise ValueError('Correction must only add JSON-object guards without changing reads, output or query scope')
    added = new_guards - old_guards
    selected = []
    for site in reversed(sites):
        if added[site['key']]:
            added[site['key']] -= 1
            selected.append({k: v for k, v in site.items() if k != 'key'})
    return {'argv': shlex.split(after), 'kind': 'json-object-guard', 'sites': selected}


def segments(command):
    """Split only actual shell pipes, preserving quoted regex characters."""
    result, start, quote, escape = [], 0, None, False
    for i, c in enumerate(command):
        if escape:
            if c == '\n':
                return None
            escape = False
            continue
        if c == '\\' and quote != "'":
            escape = True
            continue
        if quote:
            if c == quote:
                quote = None
            elif quote == '"' and c in '`$':
                return None
            continue
        if c in "'\"":
            quote = c
        elif c == '|':
            result.append(command[start:i]); start = i + 1
        elif c in '\n;&><`$()':
            return None
    if quote or escape:
        return None
    result.append(command[start:])
    return result if all(p.strip() for p in result) else None


def simple_read(command):
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args or any(a == '--pre' or a.startswith('--pre=') for a in args):
        return False
    if args[0] in READ:
        return True
    if args[0] == 'sed':
        return len(args) >= 4 and args[1] == '-n' and re.fullmatch(r'(?:[0-9]+|\$)(?:,(?:[0-9]+|\$))?p', args[2]) is not None and all(not a.startswith('-') for a in args[3:])
    if args[0] == 'git':
        if any(a.startswith(('--output', '--ext-diff', '--textconv', '--exec-path')) for a in args):
            return False
        return len(args) > 1 and (args[1] in GIT or args[1:3] in [['branch', '--list'], ['worktree', 'list']])
    return False


def readonly_command(command):
    python = query_python(command)
    if python is not None:
        return python
    parts = segments(command)
    return command if parts and all(simple_read(p) for p in parts) else None
