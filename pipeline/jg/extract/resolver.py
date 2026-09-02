"""Resolution: extracted references become ECLIs and provision ids.

An unresolved reference is not an error. PLAN.md section 7 makes it a coverage metric:
it is counted and reported, never silently dropped.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from jg.db import Conn
from jg.models import Reference, ReferenceKind

log = logging.getLogger(__name__)

#: ``Reference.resolution_basis`` values. How the resolver arrived at an act number.
BASIS_EXPLICIT = "explicit"
BASIS_BACKWARD_SCAN = "backward-scan"
BASIS_DOCUMENT_MODE = "document-mode"


def normalize_alias(raw: str) -> str:
    """The one normalisation rule for ``decision_alias.alias``.

    Both sides of the lookup must use this function: the crawler/normaliser when it writes
    alias rows, and the resolver when it reads them. ``decision_alias.alias`` is therefore
    always stored in normalised form.

    The rule is deliberately minimal and lossless in the ways that matter:

    * non-breaking and other unicode spaces collapse to single ASCII spaces,
    * leading and trailing whitespace is stripped,
    * case is folded, so ``ECLI:CZ:NSS:...`` and ``ecli:cz:nss:...`` are one key.

    Punctuation is left alone. ``6 Ads 45/2014`` and ``6 Ads 45/2014-32`` stay two distinct
    aliases, which is correct: the sheet number is part of the č. j.
    """
    return " ".join(raw.split()).casefold()


def alias_candidates(ref: Reference) -> list[str]:
    """Normalised alias strings to try for one reference, most specific first.

    A č. j. carrying a sheet number is tried with the sheet first and without it second,
    because the corpus may hold either form and the fuller one is the better match.
    """
    raw: list[str] = []
    match ref.kind:
        case ReferenceKind.REF_NO:
            ref_no = ref.groups.get("ref_no")
            sheet_no = ref.groups.get("sheet_no")
            if ref_no and sheet_no:
                raw.append(f"{ref_no}-{sheet_no}")
            if ref_no:
                raw.append(ref_no)
        case ReferenceKind.CASE_NO:
            case_no = ref.groups.get("case_no")
            if case_no:
                raw.append(case_no)
        case ReferenceKind.ECLI:
            raw.append(ref.raw_text)
        case ReferenceKind.JOURNAL_NO:
            seq = ref.groups.get("journal_seq")
            year = ref.groups.get("journal_year")
            if seq and year:
                raw.append(f"R {seq}/{year}")
            raw.append(ref.raw_text)
        case ReferenceKind.PROVISION:
            pass

    seen: dict[str, None] = {}
    for value in raw:
        normalised = normalize_alias(value)
        if normalised:
            seen.setdefault(normalised, None)
    return list(seen)


@dataclass
class ResolutionStats:
    """Coverage metric for one extraction run. PLAN.md section 7."""

    resolved: Counter[str] = field(default_factory=Counter)
    unresolved: Counter[str] = field(default_factory=Counter)
    #: How provision act numbers were arrived at: explicit / backward-scan / document-mode.
    basis: Counter[str] = field(default_factory=Counter)

    def record(self, ref: Reference, *, resolved: bool) -> None:
        (self.resolved if resolved else self.unresolved)[str(ref.kind)] += 1
        if ref.resolution_basis:
            self.basis[ref.resolution_basis] += 1

    def merge(self, other: ResolutionStats) -> None:
        self.resolved.update(other.resolved)
        self.unresolved.update(other.unresolved)
        self.basis.update(other.basis)

    @property
    def total(self) -> int:
        return sum(self.resolved.values()) + sum(self.unresolved.values())

    @property
    def coverage(self) -> float:
        """Fraction of references that resolved. 1.0 when there was nothing to resolve."""
        return 1.0 if self.total == 0 else sum(self.resolved.values()) / self.total

    def kinds(self) -> list[str]:
        return sorted(set(self.resolved) | set(self.unresolved))

    def coverage_for(self, kind: str) -> float:
        total = self.resolved[kind] + self.unresolved[kind]
        return 1.0 if total == 0 else self.resolved[kind] / total


def resolve_decision(ref: Reference, conn: Conn) -> str | None:
    """Look one reference up in ``decision_alias`` and set ``Reference.resolved_ecli``.

    Returns the ECLI, or ``None`` when the reference is not in the corpus. Not in the
    corpus is a normal state, not an error.
    """
    resolve_decisions([ref], conn)
    return ref.resolved_ecli


def resolve_decisions(refs: Sequence[Reference], conn: Conn) -> None:
    """Batch form of :func:`resolve_decision`: one query for a whole document."""
    decision_refs = [r for r in refs if r.kind is not ReferenceKind.PROVISION]
    if not decision_refs:
        return

    wanted: set[str] = set()
    for ref in decision_refs:
        wanted.update(alias_candidates(ref))
    if not wanted:
        return

    rows = conn.execute(
        "select alias, ecli from decision_alias where alias = any(%s)",
        (sorted(wanted),),
    ).fetchall()
    by_alias = {row["alias"]: row["ecli"] for row in rows}

    for ref in decision_refs:
        for candidate in alias_candidates(ref):
            ecli = by_alias.get(candidate)
            if ecli is not None:
                ref.resolved_ecli = ecli
                break
        else:
            log.debug(
                "unresolved %s %r in paragraph %s", ref.kind, ref.raw_text, ref.paragraph_idx
            )


def assign_provision_acts(refs: Sequence[Reference]) -> None:
    """Provision anaphora, as a pure function over one document's references.

    ``§ 2000 odst. 1`` usually omits the act. Fill it in and record how, so the basis is
    traceable per PLAN.md section 7:

    * ``explicit`` — the act number was written out next to the section.
    * ``backward-scan`` — taken from the most recent explicit act *earlier* in the document.
    * ``document-mode`` — no earlier explicit act, so the act cited most often in this
      document is used.

    A reference with neither an earlier act nor a document mode keeps
    ``resolution_basis = None`` and stays unresolved. The inferred act is written back into
    ``Reference.groups['act_no']`` so downstream stages see one shape regardless of basis.
    """
    ordered = sorted(refs, key=lambda r: (r.paragraph_idx, r.start, r.end))
    provisions = [r for r in ordered if r.kind is ReferenceKind.PROVISION]
    if not provisions:
        return

    explicit_counts = Counter(
        act for r in provisions if (act := r.groups.get("act_no")) is not None
    )
    document_mode = explicit_counts.most_common(1)[0][0] if explicit_counts else None

    most_recent: str | None = None
    for ref in provisions:
        explicit = ref.groups.get("act_no")
        if explicit is not None:
            ref.resolution_basis = BASIS_EXPLICIT
            most_recent = explicit
            continue
        if most_recent is not None:
            ref.groups["act_no"] = most_recent
            ref.resolution_basis = BASIS_BACKWARD_SCAN
        elif document_mode is not None:
            ref.groups["act_no"] = document_mode
            ref.resolution_basis = BASIS_DOCUMENT_MODE
        else:
            continue
        log.info(
            "provision %r in paragraph %s resolved to act %s by %s",
            ref.raw_text,
            ref.paragraph_idx,
            ref.groups["act_no"],
            ref.resolution_basis,
        )


def provision_key(ref: Reference) -> tuple[str, str, str | None] | None:
    """``(act_no, section, subsec)`` for a provision reference, or None if unresolvable."""
    if ref.kind is not ReferenceKind.PROVISION:
        return None
    act_no = ref.groups.get("act_no")
    section = ref.groups.get("section")
    if act_no is None or section is None:
        return None
    return act_no, section, ref.groups.get("subsec")


def resolve_provisions(refs: Sequence[Reference], conn: Conn) -> None:
    """Resolve provision references to ``provision.id``.

    Runs :func:`assign_provision_acts` first, then looks the resulting keys up. Read-only:
    provision rows are created by the e-Sbírka ingest and, for citations, by the extract
    runner — never here, so that resolution has no write side effects.
    """
    assign_provision_acts(refs)

    keys = {key for ref in refs if (key := provision_key(ref)) is not None}
    if not keys:
        return

    rows = conn.execute(
        """
        select id, act_no, section, subsec
          from provision
         where act_no = any(%s) and section = any(%s)
        """,
        (sorted({k[0] for k in keys}), sorted({k[1] for k in keys})),
    ).fetchall()
    by_key = {(row["act_no"], row["section"], row["subsec"]): row["id"] for row in rows}

    for ref in refs:
        key = provision_key(ref)
        if key is None:
            continue
        provision_id = by_key.get(key)
        if provision_id is not None:
            ref.resolved_provision_id = provision_id
        else:
            log.debug("provision %s not in corpus for %r", key, ref.raw_text)


def resolve_references(refs: Sequence[Reference], conn: Conn) -> ResolutionStats:
    """Resolve one document's references and report coverage by kind."""
    resolve_decisions(refs, conn)
    resolve_provisions(refs, conn)
    return summarise(refs)


def summarise(refs: Iterable[Reference]) -> ResolutionStats:
    """Count resolved vs unresolved by kind. Pure; no database access."""
    stats = ResolutionStats()
    for ref in refs:
        resolved = (
            ref.resolved_provision_id is not None
            if ref.kind is ReferenceKind.PROVISION
            else ref.resolved_ecli is not None
        )
        stats.record(ref, resolved=resolved)
    return stats
