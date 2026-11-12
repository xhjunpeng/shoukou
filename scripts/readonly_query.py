"""Recognize bounded read-only queries; never authorize arbitrary Python."""
import ast
import builtins
import re
import shlex
import shutil
import sys
from pathlib import Path

READ = {'pwd', 'ls', 'cat', 'rg', 'head', 'tail', 'wc', 'which', 'true', 'printf'}
GIT = {'status', 'diff', 'log', 'show', 'ls-files', 'ls-tree', 'rev-parse', 'show-ref', 'for-each-ref', 'merge-base'}
CALLS = {'Path', 'print', 'str', 'len', 'list', 'dict', 'all', 'any', 'sorted', 'tuple', 'set', 'int', 'bool'}
ATTRS = {'read_text', 'read_bytes', 'exists', 'is_file', 'is_dir', 'glob', 'rglob', 'get', 'items', 'values', 'keys', 'loads', 'dumps', 'sha256', 'hexdigest', 'join', 'name', 'parent', 'stem', 'suffix', 'parts'}


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
