"""The suite's own layout, enforced instead of remembered (docs/testing.md "Layout").

The tests used to sit in one flat directory of 91 files. Splitting them by domain only stays
readable if three properties hold, and each of them fails *quietly* rather than loudly:

1. **Basenames are unique across the tree.** The project ships no `__init__.py`, so pytest's
   `prepend` import mode imports every test module by its bare basename. Two `test_report.py`
   in different folders are the same module name — the second one collides at collection with
   an import error that names neither file usefully.
2. **No `__init__.py` creeps in.** Adding one to silence such a collision would turn `tests/`
   into a package and quietly change how every module is imported.
3. **Nothing new lands at the root.** A flat root is exactly what the split replaced, and one
   stray file there is how it grows back.
"""
import pathlib
from typing import Dict, List

_TESTS = pathlib.Path(__file__).resolve().parents[1]

# `conftest.py` is pytest's own per-directory hook and is expected at the root; everything else
# there would be an un-filed test.
_ROOT_ALLOWED = {'conftest.py'}

# `tests/` holds test modules and pytest's own hooks, nothing else — the split deliberately
# outsourced no helpers (a factory that varies per case stays with its test). Extend this set
# rather than dropping a bare module into the tree, so the exception is a decision on the record.
_NON_TEST_ALLOWED = {'conftest.py'}


def _test_files() -> List[pathlib.Path]:
    return [p for p in _TESTS.rglob('test_*.py') if '__pycache__' not in p.parts]


def test_basenames_are_unique_across_the_tree() -> None:
    seen: Dict[str, List[str]] = {}
    for path in _test_files():
        seen.setdefault(path.name, []).append(str(path.relative_to(_TESTS)))

    clashes = {name: paths for name, paths in seen.items() if len(paths) > 1}
    assert not clashes, (
        'test basenames must be unique across tests/ — pytest imports them by basename:\n'
        + '\n'.join(f'  {name}: {", ".join(paths)}' for name, paths in sorted(clashes.items())))


def test_no_init_files_under_tests() -> None:
    found = [str(p.relative_to(_TESTS)) for p in _TESTS.rglob('__init__.py')]
    assert not found, f'tests/ must stay package-free (project convention): {found}'


def test_every_test_file_sits_in_a_category_folder() -> None:
    # A test file directly in tests/ has no category — the state the split removed.
    stray = [p.name for p in _TESTS.glob('*.py')
             if p.name not in _ROOT_ALLOWED and p.name.startswith('test_')]
    assert not stray, (
        'these belong in a category folder (create one if none fits — docs/testing.md): '
        f'{stray}')


def test_no_module_hides_from_collection() -> None:
    """A file that lost its `test_` prefix is silently no longer collected.

    The three checks above all search with `rglob('test_*.py')`, so none of them can see the one
    mistake a *move* actually invites: a file renamed to `breaking_report_test.py`, or stripped of
    the prefix entirely, drops out of pytest's collection (`python_files = test_*.py`) and out of
    this suite's own census at the same moment. The result is a green run with less coverage —
    the same failure shape the root check exists for, one level less visible.

    So this is the one check that has to look at every `.py` under `tests/` rather than at the
    collected ones, and it is deliberately strict: an intentional non-test module joins
    `_NON_TEST_ALLOWED` above.
    """
    uncollected = sorted(str(p.relative_to(_TESTS)) for p in _TESTS.rglob('*.py')
                         if '__pycache__' not in p.parts
                         and not p.name.startswith('test_')
                         and p.name not in _NON_TEST_ALLOWED)
    assert not uncollected, (
        'these are not collected by pytest (python_files = test_*.py) — rename them to test_*.py '
        f'or add them to _NON_TEST_ALLOWED with a reason: {uncollected}')
