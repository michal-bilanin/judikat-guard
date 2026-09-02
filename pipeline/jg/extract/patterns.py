"""The rules pass: compiled citation patterns loaded from ``extract/patterns.toml``.

``extract/patterns.toml`` is the single source of truth shared with the Java runtime
(PLAN.md section 7). This module never hard-codes a regex; it only compiles what the TOML
file declares, so the two implementations cannot drift.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from jg.config import PATTERNS_TOML
from jg.models import Reference, ReferenceKind


@dataclass(frozen=True)
class CompiledPattern:
    """One ``[patterns.*]`` table from the TOML, compiled once."""

    name: str
    kind: ReferenceKind
    regex: re.Pattern[str]
    #: Capture-group names, positional: ``groups[N - 1]`` names capture group ``N``.
    groups: tuple[str, ...]

    def find(self, text: str, paragraph_idx: int) -> list[Reference]:
        """Every match of this one pattern, in text order, before the overlap policy."""
        found: list[Reference] = []
        for match in self.regex.finditer(text):
            start, end = match.span()
            found.append(
                Reference(
                    kind=self.kind,
                    raw_text=match.group(0),
                    start=start,
                    end=end,
                    paragraph_idx=paragraph_idx,
                    groups=self._named_groups(match),
                )
            )
        return found

    def _named_groups(self, match: re.Match[str]) -> dict[str, str]:
        """Map capture group N to ``groups[N - 1]``, skipping groups that did not match.

        A group that did not participate in the match is absent from the dict rather than
        present with a ``None`` value, because ``Reference.groups`` is ``dict[str, str]``
        and "the act number was not written out" is exactly the absence the resolver's
        anaphora pass looks for.
        """
        named: dict[str, str] = {}
        for position, name in enumerate(self.groups, start=1):
            if position > match.re.groups:
                break
            value = match.group(position)
            if value is not None:
                named[name] = value
        return named


@dataclass(frozen=True)
class PatternSet:
    """Everything ``extract/patterns.toml`` declares, compiled and frozen."""

    version: int
    #: In TOML declaration order. That order decides ties in the overlap policy.
    patterns: tuple[CompiledPattern, ...]
    #: ``[triggers].citation``: the only input to the anaphora model pass.
    triggers: tuple[str, ...]
    #: ``[markers]``: party_submission / departure / quashing, for the classifier's router.
    markers: dict[str, tuple[str, ...]]

    def by_name(self, name: str) -> CompiledPattern:
        for pattern in self.patterns:
            if pattern.name == name:
                return pattern
        raise KeyError(f"no pattern named {name!r} in {PATTERNS_TOML}")


def load_patterns(path: Path = PATTERNS_TOML) -> PatternSet:
    """Parse and compile ``extract/patterns.toml``. Uncached; see :func:`pattern_set`."""
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    compiled: list[CompiledPattern] = []
    for name, table in raw.get("patterns", {}).items():
        compiled.append(
            CompiledPattern(
                name=name,
                kind=ReferenceKind(table["kind"]),
                regex=re.compile(table["regex"]),
                groups=tuple(table.get("groups", ())),
            )
        )

    markers = {
        key: tuple(values)
        for key, values in raw.get("markers", {}).items()
        if isinstance(values, list)
    }
    return PatternSet(
        version=int(raw.get("version", 0)),
        patterns=tuple(compiled),
        triggers=tuple(raw.get("triggers", {}).get("citation", ())),
        markers=markers,
    )


@cache
def pattern_set() -> PatternSet:
    """Module-level singleton. Compiles the TOML once per process."""
    return load_patterns()


def find_references(
    text: str, paragraph_idx: int, patterns: PatternSet | None = None
) -> list[Reference]:
    """Every citation-like span in one paragraph, in document order.

    ``paragraph_idx`` is 1-based and matches ``decision_paragraph.idx``. ``Reference.start``
    and ``Reference.end`` are offsets into ``text``, not into the whole document.
    """
    active = patterns or pattern_set()
    candidates: list[Reference] = []
    for pattern in active.patterns:
        candidates.extend(pattern.find(text, paragraph_idx))
    kept = drop_contained(candidates)
    kept.sort(key=lambda ref: (ref.start, ref.end))
    return kept


def drop_contained(candidates: Sequence[Reference]) -> list[Reference]:
    """Overlap policy for spans produced by different patterns.

    Two patterns can fire over the same text — ``case_no_us`` and ``case_no_gen`` are both
    anchored on ``sp. zn.``, and the ``provision`` act-number tail can swallow a shorter
    citation that sits inside it. The rule is:

    * **Keep the longest match.** A span *fully contained* in a strictly longer span is
      dropped, whatever pattern produced it.
    * **Partial overlap is kept.** Two spans that merely cross are both real references;
      dropping either would lose a citation.
    * **Identical spans: the pattern declared first in ``extract/patterns.toml`` wins.**
      Declaration order is the tie-break so the outcome is stable and reviewable in the
      TOML rather than dependent on dict iteration.

    ``candidates`` must be in pattern-declaration order, which is what
    :func:`find_references` produces.
    """
    kept: list[Reference] = []
    for position, candidate in enumerate(candidates):
        if any(
            _covers(other, candidate, other_position < position)
            for other_position, other in enumerate(candidates)
            if other_position != position
        ):
            continue
        kept.append(candidate)
    return kept


def _covers(outer: Reference, inner: Reference, outer_declared_first: bool) -> bool:
    """True when ``outer`` suppresses ``inner`` under the overlap policy."""
    if outer.paragraph_idx != inner.paragraph_idx:
        return False
    if outer.start > inner.start or outer.end < inner.end:
        return False
    outer_length = outer.end - outer.start
    inner_length = inner.end - inner.start
    if outer_length > inner_length:
        return True
    return outer_declared_first


def triggers(patterns: PatternSet | None = None) -> tuple[str, ...]:
    """Anaphora trigger phrases, e.g. *citovaného rozhodnutí*, *tamtéž*."""
    return (patterns or pattern_set()).triggers


def markers(name: str, patterns: PatternSet | None = None) -> tuple[str, ...]:
    """One ``[markers]`` list: ``party_submission``, ``departure`` or ``quashing``."""
    return (patterns or pattern_set()).markers.get(name, ())
