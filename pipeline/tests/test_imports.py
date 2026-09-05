"""Every module must import cleanly as the *first* import of a fresh interpreter.

This is not a style check. ``jg.gemini`` once imported the provider seam from
``jg.classify.reasoning``, which pulled in the ``jg.classify`` package, whose ``__init__``
imports ``router``, which imports ``jg.gemini`` — a cycle. The whole suite still passed,
because pytest collects ``tests/`` in a fixed order and something always imported
``jg.classify`` first, leaving a fully built module in ``sys.modules`` by the time
``jg.gemini`` was reached. The failure only appeared outside pytest: a script, a REPL, or
``python -m jg.provisions`` starting from ``jg.gemini`` got a partially initialised module
and an ``ImportError``.

Each module therefore gets its own subprocess. Importing them in one process would prove
nothing — the first successful import populates ``sys.modules`` and hides the next cycle,
which is exactly the trap that let the original bug through.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: Every module a person or a script might reasonably import first.
MODULES = [
    "jg.cli",
    "jg.classify",
    "jg.classify.reasoning",
    "jg.classify.router",
    "jg.classify.structural",
    "jg.config",
    "jg.crawl",
    "jg.db",
    "jg.extract",
    "jg.gemini",
    "jg.llm_types",
    "jg.models",
    "jg.normalize",
    "jg.provisions",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"`import {module}` fails when it is the first import of a fresh interpreter, "
        f"which is how a script or a REPL reaches it:\n{result.stderr}"
    )


def test_the_provider_seam_has_no_jg_imports() -> None:
    """The seam is a leaf module, which is what makes the cycle structurally impossible.

    Stronger than re-testing the import order: a leaf with no ``jg`` imports cannot take
    part in a cycle at all, so this asserts the property rather than one of its symptoms.
    """
    from pathlib import Path

    source = Path(__import__("jg.llm_types", fromlist=["x"]).__file__ or "").read_text(
        encoding="utf-8"
    )
    offending = [
        line
        for line in source.splitlines()
        if line.startswith(("import jg", "from jg"))
    ]
    assert not offending, f"jg.llm_types must import nothing from jg, found: {offending}"
