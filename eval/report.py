#!/usr/bin/env python3
"""Evaluation harness. PLAN.md section 15, milestone M8.

Prints, in this order:

  1. extraction precision / recall against ``eval/extraction-golden.jsonl``
  2. per-label precision / recall for treatment classification: ``eval/labels.csv``
     joined against the ``treatment`` table
  3. a confusion matrix, with the DISTINGUISHED / NARROWED / DEPARTED cluster marked,
     because that cluster is where the reasoning model earns its keep
  4. red-light precision on its own, because per D8 that is the number we tune for,
     knowingly at the cost of recall

Stdlib plus ``jg`` only: no pandas, no sklearn. This runs long before there is any
data, so every missing input is reported as a missing input and the process still
exits 0. Nothing here fails a build; the cross-runtime parity test does that.

The report is internal tooling for the team and the results slide, so its headings
stay in English and use the label enum of PLAN.md section 8 verbatim. The Czech-only
rule governs user-facing product output, not this table.

    python eval/report.py                    # make eval
    python eval/report.py --extraction-only  # make eval-extract
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- paths -----------------------------------------------------------------
# jg.config owns the paths, but the report must also run outside the venv, so fall
# back to the repo layout rather than dying on an ImportError.
try:
    from jg.config import EVAL_DIR, PATTERNS_TOML, db_url
except ImportError:  # pragma: no cover - exercised only without the venv
    _ROOT = Path(__file__).resolve().parents[1]
    EVAL_DIR = _ROOT / "eval"
    PATTERNS_TOML = _ROOT / "extract" / "patterns.toml"

    def db_url() -> str:
        return os.environ.get(
            "JG_DB_URL", "postgresql://judikat:judikat@localhost:55432/judikat"
        )


# --- label vocabulary ------------------------------------------------------
try:
    from jg.models import TreatmentLabel

    LABELS: tuple[str, ...] = tuple(label.value for label in TreatmentLabel)
except ImportError:  # pragma: no cover
    LABELS = (
        "FOLLOWED",
        "MENTIONED",
        "DISTINGUISHED",
        "NARROWED",
        "DEPARTED",
        "CRITICIZED",
        "QUASHED",
        "UNCLASSIFIED",
    )

#: PLAN.md section 15: the cluster the reasoning model exists for.
CLUSTER = ("DISTINGUISHED", "NARROWED", "DEPARTED")

#: Printed in the confusion matrix for a gold row the pipeline never labelled.
NO_PRED = "(none)"

#: A row is a placeholder, not ground truth, if either side carries this prefix.
TEST_PREFIX = "TEST-"

WIDTH = 78


# --- small formatting helpers ----------------------------------------------


def rule(char: str = "=") -> str:
    return char * WIDTH


def heading(text: str) -> None:
    print()
    print(text)
    print(rule("-"))


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:0.2f}"


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Sequence[str] | None = None,
) -> list[str]:
    """Fixed-width columns. Wide enough to read aloud, narrow enough for a slide."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    align = list(aligns) if aligns else ["<"] * len(headers)

    def line(cells: Sequence[str]) -> str:
        return "  ".join(f"{c:{align[i]}{widths[i]}}" for i, c in enumerate(cells)).rstrip()

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out.extend(line(r) for r in rows)
    return out


def emit(lines: Sequence[str], indent: str = "  ") -> None:
    for line in lines:
        print(indent + line)


# --- gold labels -----------------------------------------------------------


@dataclass(frozen=True)
class GoldRow:
    citing_ecli: str
    cited_ecli: str
    paragraph_idx: int | None
    gold_label: str
    note: str
    line_no: int

    @property
    def key(self) -> tuple[str, str, int | None]:
        return (self.citing_ecli, self.cited_ecli, self.paragraph_idx)


@dataclass
class LabelFile:
    path: Path
    rows: list[GoldRow]
    placeholders: int
    commented: int
    problems: list[str]
    exists: bool


def load_labels(path: Path) -> LabelFile:
    if not path.exists():
        return LabelFile(path, [], 0, 0, [f"{path} does not exist"], exists=False)

    text = path.read_text(encoding="utf-8")
    # '#' comments are not CSV, so strip them before the reader sees them.
    lines = text.splitlines()
    body = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    commented = sum(
        1 for ln in lines if ln.lstrip().startswith("#") and TEST_PREFIX in ln and "," in ln
    )
    rows: list[GoldRow] = []
    problems: list[str] = []
    placeholders = 0

    reader = csv.DictReader(body)
    expected = ["citing_ecli", "cited_ecli", "paragraph_idx", "gold_label", "note"]
    if reader.fieldnames is None or [f.strip() for f in reader.fieldnames] != expected:
        problems.append(f"header must be: {','.join(expected)} (got: {reader.fieldnames})")
        return LabelFile(path, [], 0, commented, problems, exists=True)

    for offset, raw in enumerate(reader, start=2):
        citing = (raw.get("citing_ecli") or "").strip()
        cited = (raw.get("cited_ecli") or "").strip()
        label = (raw.get("gold_label") or "").strip().upper()
        idx_raw = (raw.get("paragraph_idx") or "").strip()
        note = (raw.get("note") or "").strip()

        if citing.startswith(TEST_PREFIX) or cited.startswith(TEST_PREFIX):
            placeholders += 1
            continue
        if not citing or not cited:
            problems.append(f"row {offset}: citing_ecli and cited_ecli are both required")
            continue
        if label not in LABELS:
            problems.append(f"row {offset}: gold_label {label!r} is not one of {LABELS}")
            continue
        idx: int | None = None
        if idx_raw:
            try:
                idx = int(idx_raw)
            except ValueError:
                problems.append(f"row {offset}: paragraph_idx {idx_raw!r} is not an integer")
                continue
        rows.append(GoldRow(citing, cited, idx, label, note, offset))

    return LabelFile(path, rows, placeholders, commented, problems, exists=True)


# --- predictions from the database -----------------------------------------


@dataclass(frozen=True)
class Prediction:
    label: str
    confidence: float | None
    route: str | None
    prompt_version: str | None
    citing_may_depart: bool
    exact_paragraph: bool


@dataclass
class DbSnapshot:
    reachable: bool
    error: str | None
    # (citing_ecli, cited_ecli, paragraph_idx) -> prediction
    by_triple: dict[tuple[str, str, int | None], Prediction]
    # (citing_ecli, cited_ecli) -> predictions on that pair, any paragraph
    by_pair: dict[tuple[str, str], list[Prediction]]
    # (citing_ecli, cited_ecli) -> may the citing body depart from the cited court
    authority: dict[tuple[str, str], bool]
    citation_count: int
    treatment_count: int
    corpus: list[tuple[str, int, str]]


#: Explicit column lists, no select *. `citing_may_depart` is the join that turns a
#: DEPARTED label into either a red supersession or an amber conflict (PLAN.md section 6).
_EDGE_SQL = """
select c.citing_ecli,
       c.cited_ecli,
       c.paragraph_idx,
       t.label,
       t.confidence,
       t.route,
       t.prompt_version,
       t.created_at,
       coalesce(da.binding, false) as citing_may_depart
  from citation c
  join decision cd on cd.ecli = c.citing_ecli
  left join decision td on td.ecli = c.cited_ecli
  left join departure_authority da
         on da.citing_court = cd.court_code
        and da.citing_panel = cd.panel_type
        and da.cited_court  = td.court_code
  left join treatment t on t.citation_id = c.id
 where c.cited_ecli is not null
"""

_COUNT_SQL = "select (select count(*) from citation), (select count(*) from treatment)"

_CORPUS_SQL = """
select court_code, decision_count, covered_through
  from corpus_meta
 order by court_code
"""


def load_predictions(prompt_version: str | None) -> DbSnapshot:
    """Read the classified edges. Any failure degrades to an empty, unreachable snapshot."""
    empty = DbSnapshot(False, None, {}, {}, {}, 0, 0, [])
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        empty.error = f"psycopg is not installed ({exc})"
        return empty

    sql = _EDGE_SQL
    params: list[Any] = []
    if prompt_version:
        sql += " and (t.prompt_version = %s or t.label is null)"
        params.append(prompt_version)

    try:
        with psycopg.connect(db_url(), connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            edges = cur.fetchall()
            cur.execute(_COUNT_SQL)
            citation_count, treatment_count = cur.fetchone() or (0, 0)
            cur.execute(_CORPUS_SQL)
            corpus = [(c, n, str(through)) for c, n, through in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 - the report must survive any db failure
        empty.error = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0]}"
        return empty

    snap = DbSnapshot(True, None, {}, {}, {}, citation_count, treatment_count, corpus)
    # Latest created_at wins when several prompt versions labelled the same edge.
    newest: dict[tuple[str, str, int | None], float] = {}
    for citing, cited, para, label, conf, route, pver, created, may_depart in edges:
        snap.authority[(citing, cited)] = bool(may_depart)
        if label is None:
            continue
        pred = Prediction(
            label=str(label),
            confidence=float(conf) if conf is not None else None,
            route=route,
            prompt_version=pver,
            citing_may_depart=bool(may_depart),
            exact_paragraph=True,
        )
        triple = (citing, cited, para)
        # Epoch seconds rather than datetimes: created_at is timestamptz and therefore
        # aware, and a naive fallback sentinel would blow up the comparison.
        stamp = created.timestamp() if created is not None else float("-inf")
        if triple not in newest or stamp >= newest[triple]:
            newest[triple] = stamp
            snap.by_triple[triple] = pred
        snap.by_pair.setdefault((citing, cited), []).append(pred)
    return snap


def match_prediction(gold: GoldRow, snap: DbSnapshot) -> Prediction | None:
    """Exact triple first; then the pair, but only when it is unambiguous."""
    hit = snap.by_triple.get(gold.key)
    if hit is not None:
        return hit
    loose = snap.by_pair.get((gold.citing_ecli, gold.cited_ecli), [])
    if len(loose) == 1:
        one = loose[0]
        return Prediction(
            one.label,
            one.confidence,
            one.route,
            one.prompt_version,
            one.citing_may_depart,
            exact_paragraph=False,
        )
    return None


# --- extraction fixture ----------------------------------------------------


@dataclass(frozen=True)
class Span:
    kind: str
    start: int
    end: int
    raw_text: str

    def label(self) -> str:
        return f"{self.kind} [{self.start}:{self.end}] {self.raw_text!r}"


@dataclass
class GoldenDoc:
    doc_id: str
    text: str
    spans: list[Span]
    groups: dict[Span, dict[str, str]]


def load_golden(path: Path) -> tuple[list[GoldenDoc], list[str]]:
    if not path.exists():
        return [], [f"{path} does not exist"]

    docs: list[GoldenDoc] = []
    problems: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"line {line_no}: not valid JSON ({exc.msg})")
            continue
        doc_id = str(obj.get("id", f"line-{line_no}"))
        text = obj.get("text")
        if not isinstance(text, str):
            problems.append(f"{doc_id}: 'text' is missing or not a string")
            continue
        spans: list[Span] = []
        groups: dict[Span, dict[str, str]] = {}
        for item in obj.get("expected", []):
            try:
                span = Span(
                    str(item["kind"]), int(item["start"]), int(item["end"]), str(item["raw_text"])
                )
            except (KeyError, TypeError, ValueError):
                problems.append(f"{doc_id}: expected entry needs kind, raw_text, start, end")
                continue
            # The fixture is a contract; a span whose offsets do not cut out its own
            # raw_text would silently mark a correct extractor wrong.
            if text[span.start : span.end] != span.raw_text:
                problems.append(
                    f"{doc_id}: offsets {span.start}:{span.end} cut out "
                    f"{text[span.start : span.end]!r}, not {span.raw_text!r}"
                )
                continue
            spans.append(span)
            groups[span] = {str(k): str(v) for k, v in (item.get("groups") or {}).items()}
        docs.append(GoldenDoc(doc_id, text, spans, groups))
    return docs, problems


#: The entry points the report will accept from the Python extractor, in order.
_EXTRACTOR_NAMES = ("extract_references", "find_references", "extract_all", "find_all", "extract")


def find_extractor() -> tuple[Callable[..., Any] | None, str]:
    """Locate the rules-pass extractor. Absent until M2 lands; that is not an error."""
    for module_name in ("jg.extract.patterns", "jg.extract"):
        try:
            module = __import__(module_name, fromlist=["*"])
        except ImportError:
            continue
        for attr in _EXTRACTOR_NAMES:
            fn = getattr(module, attr, None)
            if callable(fn):
                return fn, f"{module_name}.{attr}"
    return None, ""


def call_extractor(fn: Callable[..., Any], text: str) -> list[Any]:
    """Tolerate the three plausible signatures rather than pinning one prematurely."""
    # paragraph_idx is 1-based (decision_paragraph.idx); it does not affect the offsets
    # this report compares, but pass a valid value anyway.
    for args, kwargs in (((text,), {}), ((text, 1), {}), ((text,), {"paragraph_idx": 1})):
        try:
            return list(fn(*args, **kwargs))
        except TypeError:
            continue
    raise TypeError("extractor did not accept (text), (text, idx) or (text, paragraph_idx=)")


def normalise(item: Any) -> tuple[Span, dict[str, str]] | None:
    """Accept a jg.models.Reference, a dataclass or a plain dict."""

    def get(name: str) -> Any:
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)

    kind, raw, start, end = get("kind"), get("raw_text"), get("start"), get("end")
    if kind is None or start is None or end is None:
        return None
    groups = get("groups") or {}
    span = Span(str(kind), int(start), int(end), str(raw) if raw is not None else "")
    return span, {str(k): str(v) for k, v in dict(groups).items()}


# --- sections --------------------------------------------------------------


def section_extraction(golden_path: Path) -> list[str]:
    """Returns the list of missing inputs it ran into."""
    missing: list[str] = []
    heading("1. EXTRACTION  (rules pass vs eval/extraction-golden.jsonl)")

    docs, problems = load_golden(golden_path)
    if not docs:
        print(f"  fixture:   MISSING  ({golden_path})")
        for p in problems:
            print(f"    - {p}")
        missing.append(f"extraction fixture at {golden_path}")
        return missing

    gold_total = sum(len(d.spans) for d in docs)
    negatives = sum(1 for d in docs if not d.spans)
    print(f"  fixture:   {golden_path}")
    print(f"  documents: {len(docs)}   expected spans: {gold_total}   no-match docs: {negatives}")
    print(f"  patterns:  {PATTERNS_TOML}")
    print(f"  integrity: {'FAIL' if problems else 'ok'} "
          f"({len(problems)} problem(s); every offset must cut out its own raw_text)")
    for p in problems:
        print(f"    - {p}")

    by_kind_gold: dict[str, int] = {}
    for doc in docs:
        for span in doc.spans:
            by_kind_gold[span.kind] = by_kind_gold.get(span.kind, 0) + 1

    fn, fn_name = find_extractor()
    if fn is None:
        print()
        print("  extractor: NOT AVAILABLE - no importable Python rules pass yet (M2).")
        print("             Expected: jg.extract.patterns.extract_references(text) ->")
        print("             list[Reference] (jg.models.Reference). Fixture inventory only:")
        emit(
            render_table(
                ["kind", "expected spans"],
                [[k, str(v)] for k, v in sorted(by_kind_gold.items())],
                ["<", ">"],
            ),
            indent="    ",
        )
        missing.append("the Python rules-pass extractor (jg.extract.patterns), lands in M2")
        return missing

    tp = fp = fn_count = 0
    group_mismatch = 0
    per_kind: dict[str, list[int]] = {}
    failures: list[str] = []

    for doc in docs:
        try:
            raw_items = call_extractor(fn, doc.text)
        except Exception as exc:  # noqa: BLE001 - a broken extractor is a finding, not a crash
            failures.append(f"{doc.doc_id}: extractor raised {type(exc).__name__}: {exc}")
            fn_count += len(doc.spans)
            for span in doc.spans:
                per_kind.setdefault(span.kind, [0, 0, 0])[2] += 1
            continue

        predicted: dict[Span, dict[str, str]] = {}
        for item in raw_items:
            norm = normalise(item)
            if norm is None:
                failures.append(f"{doc.doc_id}: extractor returned an item without kind/start/end")
                continue
            predicted[norm[0]] = norm[1]

        gold_set = set(doc.spans)
        pred_set = set(predicted)
        for span in sorted(gold_set & pred_set, key=lambda s: s.start):
            tp += 1
            per_kind.setdefault(span.kind, [0, 0, 0])[0] += 1
            if predicted[span] != doc.groups[span]:
                group_mismatch += 1
                failures.append(
                    f"{doc.doc_id}: groups differ for {span.label()}: "
                    f"expected {doc.groups[span]}, got {predicted[span]}"
                )
        for span in sorted(pred_set - gold_set, key=lambda s: s.start):
            fp += 1
            per_kind.setdefault(span.kind, [0, 0, 0])[1] += 1
            failures.append(f"{doc.doc_id}: FALSE POSITIVE {span.label()}")
        for span in sorted(gold_set - pred_set, key=lambda s: s.start):
            fn_count += 1
            per_kind.setdefault(span.kind, [0, 0, 0])[2] += 1
            failures.append(f"{doc.doc_id}: MISSED {span.label()}")

    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn_count)
    print()
    print(f"  extractor: {fn_name}")
    rows = [
        [
            kind,
            str(counts[0]),
            str(counts[1]),
            str(counts[2]),
            pct(ratio(counts[0], counts[0] + counts[1])),
            pct(ratio(counts[0], counts[0] + counts[2])),
        ]
        for kind, counts in sorted(per_kind.items())
    ]
    rows.append(
        ["ALL", str(tp), str(fp), str(fn_count), pct(precision), pct(recall)]
    )
    emit(render_table(
        ["kind", "tp", "fp", "fn", "precision", "recall"],
        rows,
        ["<", ">", ">", ">", ">", ">"],
    ))
    print()
    print(f"  group mismatches on matched spans: {group_mismatch}")
    print("  acceptance bar, PLAN.md section 7: recall >= 0.80, precision >= 0.95,")
    print("  measured on the 30-document hand-checked M2 sample, not on this fixture.")
    for line in failures[:20]:
        print(f"    - {line}")
    if len(failures) > 20:
        print(f"    ... and {len(failures) - 20} more")
    return missing


def section_treatment(labels: LabelFile, snap: DbSnapshot) -> tuple[list[GoldRow], dict, list[str]]:
    """Per-label precision and recall. Returns (scored rows, predictions, missing inputs)."""
    missing: list[str] = []
    heading("2. TREATMENT CLASSIFICATION  (eval/labels.csv vs the treatment table)")

    if not labels.exists:
        print("  labels:    MISSING")
        for p in labels.problems:
            print(f"    - {p}")
        missing.append(f"the gold label file ({labels.path})")
        return [], {}, missing

    print(f"  gold rows: {len(labels.rows)}   target: 50 (PLAN.md section 15)")
    print(f"  placeholder rows refused: {labels.placeholders} live, "
          f"{labels.commented} still commented out")
    for p in labels.problems:
        print(f"    - malformed: {p}")

    if not labels.rows:
        print()
        print("  No scorable gold rows. eval/labels.csv still holds only the placeholder")
        print("  block; the 50 hand-labelled rows land after M1, once decision.ecli values")
        print("  exist to point at. Nothing to score, so no numbers are printed.")
        missing.append(f"50 hand-labelled rows in {labels.path} (only placeholders present)")
        return [], {}, missing

    if not snap.reachable:
        print()
        print("  Database unreachable, so no predictions to join against.")
        missing.append("a reachable database for the treatment join")
        return labels.rows, {}, missing

    preds: dict[tuple[str, str, int | None], Prediction | None] = {}
    for row in labels.rows:
        preds[row.key] = match_prediction(row, snap)

    matched = [r for r in labels.rows if preds[r.key] is not None]
    loose = [r for r in matched if not preds[r.key].exact_paragraph]  # type: ignore[union-attr]
    unmatched = [r for r in labels.rows if preds[r.key] is None]
    print(f"  joined:    {len(matched)}/{len(labels.rows)} gold rows have a stored treatment "
          f"({len(loose)} matched on the pair, not the paragraph)")
    if unmatched:
        print(f"  unlabelled: {len(unmatched)} gold row(s) counted as {NO_PRED}: a miss for the")
        print("             gold label's recall, never a hit for anyone's precision")
        missing.append(f"{len(unmatched)} gold edge(s) not present or not classified in the db")

    stats: dict[str, list[int]] = {label: [0, 0, 0] for label in LABELS}  # tp, fp, fn
    for row in labels.rows:
        pred = preds[row.key]
        predicted_label = pred.label if pred else NO_PRED
        if predicted_label == row.gold_label:
            stats[row.gold_label][0] += 1
        else:
            stats[row.gold_label][2] += 1
            if predicted_label in stats:
                stats[predicted_label][1] += 1

    rows_out = []
    for label in LABELS:
        tp, fp, fn = stats[label]
        support = tp + fn
        if support == 0 and tp + fp == 0:
            continue
        prec = ratio(tp, tp + fp)
        rec = ratio(tp, support)
        f1 = None
        if prec is not None and rec is not None and (prec + rec) > 0:
            f1 = 2 * prec * rec / (prec + rec)
        star = "*" if label in CLUSTER else " "
        rows_out.append(
            [star + label, str(support), str(tp), str(fp), str(fn), pct(prec), pct(rec), pct(f1)]
        )
    print()
    emit(render_table(
        ["label", "gold", "tp", "fp", "fn", "precision", "recall", "f1"],
        rows_out,
        ["<", ">", ">", ">", ">", ">", ">", ">"],
    ))
    print()
    print("  * DISTINGUISHED / NARROWED / DEPARTED: the cluster the reasoning model is for.")
    return labels.rows, preds, missing


def section_confusion(gold_rows: list[GoldRow], preds: dict) -> None:
    heading("3. CONFUSION MATRIX  (rows = gold, columns = predicted)")
    if not gold_rows or not preds:
        print("  Not enough data. Needs gold rows in eval/labels.csv and stored treatments.")
        return

    columns = [*LABELS, NO_PRED]
    counts: dict[tuple[str, str], int] = {}
    for row in gold_rows:
        pred = preds.get(row.key)
        col = pred.label if pred else NO_PRED
        counts[(row.gold_label, col)] = counts.get((row.gold_label, col), 0) + 1

    used_rows = [label for label in LABELS if any(counts.get((label, c)) for c in columns)]
    used_cols = [c for c in columns if any(counts.get((r, c)) for r in LABELS)]
    abbrev = {c: (NO_PRED if c == NO_PRED else c[:3]) for c in used_cols}

    table_rows = []
    for gold_label in used_rows:
        cells = []
        for col in used_cols:
            n = counts.get((gold_label, col), 0)
            in_cluster = gold_label in CLUSTER and col in CLUSTER
            cells.append(f"{n}*" if (in_cluster and n) else str(n))
        star = "*" if gold_label in CLUSTER else " "
        table_rows.append([star + gold_label, *cells])

    emit(render_table(
        ["gold \\ pred", *[abbrev[c] for c in used_cols]],
        table_rows,
        ["<", *[">"] * len(used_cols)],
    ))
    print()
    keys = ", ".join(f"{abbrev[c]}={c}" for c in used_cols if c != NO_PRED)
    print(f"  Column keys: {keys}")
    print(f"  * marks the {' / '.join(CLUSTER)} cluster.")

    cluster_gold = [r for r in gold_rows if r.gold_label in CLUSTER]
    if not cluster_gold:
        print("  No gold rows in the cluster yet.")
        return
    correct = inside = leaked_out = 0
    for row in cluster_gold:
        pred = preds.get(row.key)
        col = pred.label if pred else NO_PRED
        if col == row.gold_label:
            correct += 1
        elif col in CLUSTER:
            inside += 1
        else:
            leaked_out += 1
    leaked_in = sum(
        1
        for r in gold_rows
        if r.gold_label not in CLUSTER
        and (preds.get(r.key).label if preds.get(r.key) else NO_PRED) in CLUSTER
    )
    print()
    emit(render_table(
        ["cluster (DISTINGUISHED / NARROWED / DEPARTED)", "n"],
        [
            ["gold rows in cluster", str(len(cluster_gold))],
            ["exactly right", str(correct)],
            ["confused within the cluster", str(inside)],
            ["lost out of the cluster", str(leaked_out)],
            ["pulled into the cluster from outside", str(leaked_in)],
            ["cluster accuracy", pct(ratio(correct, len(cluster_gold)))],
        ],
        ["<", ">"],
    ))


def is_red(label: str, may_depart: bool) -> bool:
    """RED per PLAN.md section 9: QUASHED, or DEPARTED by a body that may depart."""
    return label == "QUASHED" or (label == "DEPARTED" and may_depart)


def section_red_light(gold_rows: list[GoldRow], preds: dict, snap: DbSnapshot) -> None:
    print()
    print(rule())
    print("  RED-LIGHT PRECISION")
    print(rule())

    scored = [r for r in gold_rows if preds.get(r.key) is not None]
    if not scored:
        print("  n/a - needs gold rows in eval/labels.csv joined to stored treatments.")
        print("  RED = QUASHED, or DEPARTED where departure_authority.binding is true.")
        print(rule())
        return

    tp = fp = fn_count = 0
    for row in scored:
        pred = preds[row.key]
        may_depart = snap.authority.get((row.citing_ecli, row.cited_ecli), pred.citing_may_depart)
        pred_red = is_red(pred.label, pred.citing_may_depart)
        gold_red = is_red(row.gold_label, may_depart)
        if pred_red and gold_red:
            tp += 1
        elif pred_red:
            fp += 1
        elif gold_red:
            fn_count += 1

    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn_count)
    print()
    print(f"      RED-LIGHT PRECISION:  {pct(precision)}      ({tp} of {tp + fp} red verdicts)")
    print("      červené světlo: zrušeno / překonáno")
    print()
    print(f"      red-light recall:     {pct(recall)}      ({fn_count} red edge(s) missed)")
    print("      Deliberate, per D8: a false red destroys trust permanently, a false")
    print("      amber costs thirty seconds. Recall is what we spend to buy precision.")
    print(rule())


# --- entry point -----------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Judikat Guard evaluation report (PLAN.md section 15)."
    )
    parser.add_argument(
        "--extraction-only",
        action="store_true",
        help="skip the treatment sections (make eval-extract)",
    )
    parser.add_argument("--labels", type=Path, default=EVAL_DIR / "labels.csv")
    parser.add_argument("--golden", type=Path, default=EVAL_DIR / "extraction-golden.jsonl")
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="score only treatments written by this prompt version, e.g. treatment-classify.v1",
    )
    args = parser.parse_args(argv)

    print(rule())
    print("  JUDIKÁT GUARD - EVALUATION REPORT")
    now = dt.datetime.now(tz=dt.UTC).astimezone().replace(microsecond=0)
    print(f"  generated {now.isoformat()}")
    if args.prompt_version:
        print(f"  prompt version filter: {args.prompt_version}")
    print(rule())

    missing = section_extraction(args.golden)

    if args.extraction_only:
        print()
        print("  Treatment sections skipped (--extraction-only).")
    else:
        snap = load_predictions(args.prompt_version)
        heading("DATA SOURCES")
        print(f"  database:  {'reachable' if snap.reachable else 'UNREACHABLE'}")
        if not snap.reachable:
            print(f"    - {snap.error or 'no connection'}")
            missing.append("a reachable database (is `make up && make migrate` done?)")
        else:
            print(f"  citations: {snap.citation_count}   treatments: {snap.treatment_count}")
            if snap.corpus:
                emit(render_table(
                    ["court", "decisions", "covered through"],
                    [[c, str(n), through] for c, n, through in snap.corpus],
                    ["<", ">", ">"],
                ))
            else:
                print("  corpus_meta is empty: no crawl has run yet (M1).")
                missing.append("a crawled corpus (corpus_meta is empty)")

        labels = load_labels(args.labels)
        gold_rows, preds, more = section_treatment(labels, snap)
        missing.extend(more)
        section_confusion(gold_rows, preds)
        section_red_light(gold_rows, preds, snap)

    if missing:
        heading("WHAT IS MISSING")
        for item in missing:
            print(f"  - {item}")
        print()
        print("  Reported as missing rather than raised. This harness is expected to run")
        print("  before the data exists, so it always exits 0; the cross-runtime parity")
        print("  test, not this report, is what fails the build.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
