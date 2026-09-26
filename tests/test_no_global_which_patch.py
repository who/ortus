"""No test may fake ``shutil.which`` through a module attribute path.

``monkeypatch.setattr("pkg.mod.shutil.which", ...)`` resolves ``pkg.mod.shutil``
to the one shared ``shutil`` module, so the fake answers for every caller in the
process, including the grind preflight that looks for check programs. Patch a
module-level resolver such as ``judge_routing._backend_binary`` instead.
"""

from __future__ import annotations

from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_BANNED = ('.shutil.which"', ".shutil.which'")


def test_no_test_patches_shutil_which_through_a_module_path() -> None:
    offenders = [
        f"{path.relative_to(_TESTS)}:{number}"
        for path in sorted(_TESTS.rglob("*.py"))
        if path.resolve() != Path(__file__).resolve()
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if any(token in line for token in _BANNED)
    ]
    assert not offenders, (
        "these lines patch shutil.which process-wide; patch a module-level "
        "resolver instead: " + ", ".join(offenders)
    )
