"""The model pass: anaphoric citations the rules pass cannot see.

Expressions like *citovaného rozhodnutí*, *tamtéž* or *výše uvedeného nálezu* refer to a
decision named earlier. PLAN.md section 7 constrains this pass hard:

* it runs **only** on sentences that contain a trigger from ``[triggers]`` in
  ``extract/patterns.toml`` and produced no rule match,
* the context window is the sentence plus the preceding three paragraphs,
* the model's answer **must name a candidate already extracted from that same document**;
  anything else is discarded.

This module owns the selection, the prompt assembly and that discard filter. The model call
itself is an injectable callable defaulting to ``None``, so every function here runs, and is
tested, without an API key.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import NamedTuple

from jg.extract.patterns import PatternSet, triggers
from jg.extract.resolver import alias_candidates, normalize_alias
from jg.models import Reference, ReferenceKind

log = logging.getLogger(__name__)

#: How many preceding paragraphs go into the context window. PLAN.md section 7.
CONTEXT_PARAGRAPHS = 3

#: prompt/response contract version for this pass. Assembled in code rather than in
#: prompts/ because no versioned anaphora template exists yet; see the report.
PROMPT_VERSION = "anaphora-select.v1"

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_LAST_TOKEN = re.compile(r"(\S+)\Z")
_NUMBERED_ABBREVIATION = re.compile(r"\A[0-9IVXivx]+\.\Z")

#: Tokens that end in a full stop without ending a sentence. Czech legal prose is dense
#: with them; splitting on every ". " would shred every citation in the corpus.
_ABBREVIATIONS = frozenset(
    {
        "sp.",
        "zn.",
        "č.",
        "j.",
        "odst.",
        "písm.",
        "pís.",
        "zák.",
        "sb.",
        "čl.",
        "resp.",
        "tj.",
        "tzv.",
        "např.",
        "mj.",
        "str.",
        "obč.",
        "obch.",
        "tr.",
        "usn.",
        "roz.",
        "pl.",
        "atd.",
        "apod.",
        "s.",
        "ř.",
        "z.",
        "o.",
        "sr.",
    }
)


class Candidate(NamedTuple):
    """One sentence handed to the model, with its context window."""

    paragraph_idx: int
    sentence: str
    context: str


class Resolution(NamedTuple):
    """One accepted model answer: a sentence pointed at a decision already in the document."""

    paragraph_idx: int
    sentence: str
    ecli: str


#: prompt -> the ECLIs (or raw aliases) the model believes the sentence refers to.
AnaphoraModel = Callable[[str], Sequence[str]]


def _ends_with_abbreviation(head: str) -> bool:
    match = _LAST_TOKEN.search(head)
    if match is None:
        return False
    token = match.group(1)
    return token.casefold() in _ABBREVIATIONS or bool(_NUMBERED_ABBREVIATION.match(token))


def split_sentences(text: str) -> list[tuple[int, str]]:
    """Split a paragraph into ``(start_offset, sentence)`` pairs.

    Offsets are into ``text``, so a sentence can be compared against ``Reference.start`` /
    ``Reference.end`` without re-searching. Breaks after a known abbreviation or after a
    numeral-plus-dot (``12. 3. 2015``, ``II.``) are suppressed.
    """
    breaks: list[tuple[int, int]] = []
    for match in _SENTENCE_BREAK.finditer(text):
        if _ends_with_abbreviation(text[: match.start()]):
            continue
        breaks.append(match.span())

    sentences: list[tuple[int, str]] = []
    start = 0
    for break_start, next_start in breaks:
        chunk = text[start:break_start]
        if chunk.strip():
            sentences.append((start, chunk))
        start = next_start
    tail = text[start:]
    if tail.strip():
        sentences.append((start, tail))
    return sentences


def has_trigger(sentence: str, phrases: Sequence[str]) -> bool:
    """True when the sentence contains one of the ``[triggers]`` phrases, case-insensitively."""
    folded = sentence.casefold()
    return any(phrase.casefold() in folded for phrase in phrases)


def context_window(paragraphs: Sequence[str], paragraph_idx: int, sentence: str) -> str:
    """The sentence plus the preceding three paragraphs, oldest first.

    ``paragraph_idx`` is 1-based, matching ``decision_paragraph.idx``.
    """
    end = max(paragraph_idx - 1, 0)
    start = max(end - CONTEXT_PARAGRAPHS, 0)
    preceding = [p.strip() for p in paragraphs[start:end] if p.strip()]
    return "\n\n".join([*preceding, sentence.strip()])


def candidate_sentences(
    paragraphs: Sequence[str],
    refs: Sequence[Reference],
    patterns: PatternSet | None = None,
) -> list[Candidate]:
    """Sentences that carry a trigger phrase but produced no rule match.

    A sentence is excluded when any extracted reference overlaps it, because the rules pass
    already covered that sentence and the model has nothing to add.
    """
    phrases = triggers(patterns)
    if not phrases:
        return []

    by_paragraph: dict[int, list[Reference]] = defaultdict(list)
    for ref in refs:
        by_paragraph[ref.paragraph_idx].append(ref)

    selected: list[Candidate] = []
    for paragraph_idx, body in enumerate(paragraphs, start=1):
        paragraph_refs = by_paragraph.get(paragraph_idx, ())
        for start, sentence in split_sentences(body):
            end = start + len(sentence)
            if not has_trigger(sentence, phrases):
                continue
            if any(ref.start < end and ref.end > start for ref in paragraph_refs):
                continue
            selected.append(
                Candidate(
                    paragraph_idx=paragraph_idx,
                    sentence=sentence.strip(),
                    context=context_window(paragraphs, paragraph_idx, sentence),
                )
            )
    return selected


def known_decisions(refs: Iterable[Reference]) -> dict[str, str]:
    """Every alias the model is allowed to answer with, mapped to its ECLI.

    Built from the references already extracted from *this* document that resolved. Both the
    ECLI itself and each alias form the reference matched under are accepted, so the model
    may answer with either.
    """
    allowed: dict[str, str] = {}
    for ref in refs:
        if ref.resolved_ecli is None:
            continue
        allowed[normalize_alias(ref.resolved_ecli)] = ref.resolved_ecli
        for alias in alias_candidates(ref):
            allowed.setdefault(alias, ref.resolved_ecli)
        if ref.kind is not ReferenceKind.PROVISION:
            allowed.setdefault(normalize_alias(ref.raw_text), ref.resolved_ecli)
    return allowed


def filter_model_output(proposed: Iterable[str], refs: Iterable[Reference]) -> list[str]:
    """Discard anything the model named that was not extracted from this document.

    Pure, so the hallucination gate is testable without a model. Returns canonical ECLIs,
    deduplicated, in the order the model proposed them.
    """
    allowed = known_decisions(refs)
    accepted: dict[str, None] = {}
    for value in proposed:
        ecli = allowed.get(normalize_alias(value))
        if ecli is None:
            log.warning("discarding anaphora answer %r: not extracted from this document", value)
            continue
        accepted.setdefault(ecli, None)
    return list(accepted)


def build_prompt(candidate: Candidate, refs: Sequence[Reference]) -> str:
    """Assemble the prompt for one candidate sentence.

    The allowed answers are listed in the prompt itself. That is a convenience for the
    model, not the guarantee: :func:`filter_model_output` enforces it afterwards.
    """
    options = known_decisions(refs)
    listed = sorted({ecli for ecli in options.values()})
    lines = [
        "Jsi asistent pro analýzu českých soudních rozhodnutí.",
        "",
        "V následující větě je odkaz na dříve zmíněné rozhodnutí vyjádřen nepřímo",
        "(např. „citovaného rozhodnutí“, „tamtéž“, „výše uvedeného nálezu“).",
        "Urči, na které z níže uvedených rozhodnutí věta odkazuje.",
        "",
        "Pravidla:",
        "- Odpověz výhradně identifikátorem ze seznamu kandidátů.",
        "- Pokud odkaz nelze určit, vrať prázdný seznam.",
        "- Nikdy neuváděj rozhodnutí, které v seznamu není.",
        "",
        "Kandidáti (rozhodnutí citovaná v tomto dokumentu):",
    ]
    if listed:
        lines.extend(f"- {ecli}" for ecli in listed)
    else:
        lines.append("- (žádní)")
    lines += [
        "",
        f"Kontext (odstavce {max(candidate.paragraph_idx - CONTEXT_PARAGRAPHS, 1)}"
        f"–{candidate.paragraph_idx}):",
        candidate.context,
        "",
        "Věta k vyhodnocení:",
        candidate.sentence,
        "",
        'Odpověz striktně tímto JSON: {"ecli": ["..."], "reasoning": "jedna věta"}',
    ]
    return "\n".join(lines)


def resolve_anaphora(
    paragraphs: Sequence[str],
    refs: Sequence[Reference],
    model: AnaphoraModel | None = None,
    patterns: PatternSet | None = None,
) -> list[Resolution]:
    """Run the whole pass. With ``model=None`` it selects and assembles but resolves nothing.

    That default is deliberate: ``jg extract`` must run end to end with no API key, and the
    rules-only numbers PLAN.md section 7 asks for are measured with the model switched off.
    """
    candidates = candidate_sentences(paragraphs, refs, patterns)
    if model is None:
        log.info("anaphora: %d candidate sentences, no model wired in", len(candidates))
        return []

    resolutions: list[Resolution] = []
    for candidate in candidates:
        proposed = model(build_prompt(candidate, refs))
        for ecli in filter_model_output(proposed, refs):
            resolutions.append(
                Resolution(
                    paragraph_idx=candidate.paragraph_idx,
                    sentence=candidate.sentence,
                    ecli=ecli,
                )
            )
    return resolutions
