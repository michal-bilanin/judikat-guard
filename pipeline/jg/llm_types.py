"""The provider seam: the one type and the one error every model call shares.

This module deliberately imports nothing from ``jg``. It exists because the seam is used
from both sides of the pipeline — ``jg.classify.reasoning`` (M5 treatment classification)
and ``jg.provisions`` (M6 materiality) consume it, and ``jg.gemini`` and the Anthropic
factory implement it — so wherever it lived before, somebody had to import across a package
boundary to reach it.

Concretely: it used to live in ``jg.classify.reasoning``, which made ``jg.gemini`` import
from the ``jg.classify`` package, whose ``__init__`` imports ``router``, which imports
``jg.gemini``. Nothing in that loop is wrong on its own and the test suite never saw it,
because pytest happened to import ``jg.classify`` first every time. ``import jg.gemini`` as
the first import — a script, a REPL, ``python -m`` — hit a partially initialised module and
died. A leaf module with no ``jg`` imports cannot take part in a cycle at all, which is a
cheaper guarantee than remembering the import order.

``jg.classify.reasoning`` re-exports both names, so every existing import path still works.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["ModelCall", "ModelUnavailable", "QuotaExhausted"]

#: One prompt string in, one parsed JSON object out. The smallest seam a test can stub, and
#: the reason swapping providers is a new function rather than a new pipeline: everything
#: above it — prompt assembly, the evidence-span gate, the retry, the cache, the
#: ``UNCLASSIFIED`` fallback — is provider-agnostic and stays put.
ModelCall = Callable[[str], Mapping[str, Any]]


class ModelUnavailable(RuntimeError):
    """No usable model call can be built, or no usable reply came back.

    One error type across providers, because every caller reacts to it the same way: report
    the stage as unavailable rather than guess at a label. Distinct from a *rejected* reply,
    which is a reply that arrived and failed the evidence-span gate — that is retried once
    and then recorded as ``UNCLASSIFIED`` (CLAUDE.md rule 3), never surfaced as this.
    """


class QuotaExhausted(ModelUnavailable):
    """The provider's quota is spent. Not an error in the run — the end of today's budget.

    Separated from its parent because the operator's next action is different and specific:
    wait for the window to reset and run the same command again. Nothing is wrong with the
    corpus, the prompt or the code, so the batch runner reports how far it got and stops
    cleanly rather than raising — but only a *durable* run can honestly say that, which is
    why the batch loops commit per item.

    ``retry_after`` is the provider's own hint in seconds when it gave one. Google states it
    both in the ``Retry-After`` header and in the error body as ``Please retry in 46.8s``;
    on a daily cap that figure is the wait until the next window, which can be hours.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
