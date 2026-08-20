"""The typing convention, enforced instead of remembered (CLAUDE.md "Fully typed").

Two checks over `finiexragengine/`, and they catch different things:

1. **Every parameter and every return carries an annotation** — the AST sweep that has been run by
   hand each session. Cheap, complete, needs no imports.
2. **Every annotation actually resolves.** This is the half that was missing, and it matters
   because of how modern Python behaves: since 3.14 (PEP 649) annotations are evaluated lazily, so
   a name that was never imported no longer fails at import time. `Callable` sat undefined in
   `stall_watchdog`'s `__init__` signature for weeks that way — the module imported fine, the
   suite was green, and `typing.get_type_hints()` was the only thing that would have said so.
   Building ISSUE_89 turned up a second instance (`SourcePoll` in `ingest_worker`), which is what
   made this a test rather than a note.

**The sanctioned exception is honoured.** CLAUDE.md allows `if TYPE_CHECKING:` + a string
annotation where a runtime import would cycle (`CostRecorder` in the paid-call units). Those names
are collected from the module's own `if TYPE_CHECKING:` block and treated as resolvable — flagging
them would punish the documented pattern.
"""
import ast
import importlib
import inspect
import pathlib
import typing
from typing import Dict, List, Set

_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / 'finiexragengine'


def _modules() -> List[str]:
    return sorted('.'.join(path.relative_to(_PACKAGE.parent).with_suffix('').parts)
                  for path in _PACKAGE.rglob('*.py'))


def _deferred_names(tree: ast.AST) -> Set[str]:
    """Names bound inside `if TYPE_CHECKING:` — legitimately absent at runtime."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_guard = ((isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING')
                    or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING'))
        if not is_guard:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Import, ast.ImportFrom)):
                for alias in inner.names:
                    names.add(alias.asname or alias.name.split('.')[0])
    return names


def test_every_parameter_and_return_is_annotated():
    missing: List[str] = []
    for path in sorted(_PACKAGE.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            params = args.posonlyargs + args.args + args.kwonlyargs
            for index, arg in enumerate(params):
                # `self`/`cls` only in first position — a parameter *named* self elsewhere is
                # an ordinary parameter and must be annotated like any other.
                if arg.arg in ('self', 'cls') and index == 0:
                    continue
                if arg.annotation is None:
                    missing.append(f'{path}:{node.lineno} {node.name}({arg.arg})')
            for extra in (args.vararg, args.kwarg):
                if extra is not None and extra.annotation is None:
                    missing.append(f'{path}:{node.lineno} {node.name}(*{extra.arg})')
            if node.returns is None:
                missing.append(f'{path}:{node.lineno} {node.name} -> missing return annotation')
    assert not missing, 'unannotated signatures:\n  ' + '\n  '.join(missing)


def test_every_annotation_resolves():
    # PEP 649 means an unresolvable name is silent at import time; this is what makes it speak.
    deferred: Dict[str, Set[str]] = {}
    unresolved: List[str] = []
    for path in sorted(_PACKAGE.rglob('*.py')):
        module_name = '.'.join(path.relative_to(_PACKAGE.parent).with_suffix('').parts)
        deferred[module_name] = _deferred_names(ast.parse(path.read_text(encoding='utf-8')))

    for module_name in _modules():
        module = importlib.import_module(module_name)
        # Names the module deliberately defers get a placeholder, so the sanctioned
        # TYPE_CHECKING pattern resolves here exactly as a type checker would resolve it.
        localns = {name: typing.Any for name in deferred.get(module_name, set())}
        for attribute, obj in vars(module).items():
            if getattr(obj, '__module__', None) != module_name:
                continue                       # imported from elsewhere; checked where it is defined
            functions = ([obj] if inspect.isfunction(obj)
                         else [value for value in vars(obj).values() if inspect.isfunction(value)]
                         if inspect.isclass(obj) else [])
            for function in functions:
                try:
                    typing.get_type_hints(function, localns=localns)
                except Exception as exc:       # noqa: BLE001 — any failure is the finding
                    unresolved.append(f'{module_name}.{function.__qualname__}: '
                                      f'{type(exc).__name__}: {exc}')
    assert not unresolved, (
        'annotations that do not resolve (a missing import stays silent under PEP 649):\n  '
        + '\n  '.join(unresolved))
