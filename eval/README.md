# eval/

The numbers that go on the results slide. PLAN.md section 15, milestone M8.

> **The fixtures in this directory are provisional.** `extraction-golden.jsonl` is built
> only from citation strings that appear literally in `PLAN.md` and `extract/patterns.toml`,
> because no corpus exists yet. It pins the shape of the contract, not the difficulty of
> real text. **Regrow it from crawled decisions once M1 lands**, and treat its current
> precision and recall as a smoke test, never as the measured extraction quality.
> `labels.csv` holds no ground truth at all yet, only a commented placeholder block.

```
make eval           full report: extraction + treatment + confusion + red light
make eval-extract   extraction only (python eval/report.py --extraction-only)
```

`report.py` uses the stdlib plus `jg` and nothing else. It is expected to run long before
there is data, so **it always exits 0**: a missing database, a missing extractor and an
empty label file are all reported under `WHAT IS MISSING` rather than raised. Failing the
build on cross-runtime divergence is the parity test's job, not this report's.

Useful flags: `--labels PATH`, `--golden PATH`, `--prompt-version treatment-classify.v1`
(score only the treatments written by one prompt version).

## Files

| File | What it is |
|---|---|
| `extraction-golden.jsonl` | Cross-runtime parity fixture. Both the Python and the Java extractor must reproduce it byte for byte (PLAN.md section 7). |
| `labels.csv` | Hand-labelled treatment ground truth. 50 rows after M1; placeholders only today. |
| `report.py` | Prints the table. |

## extraction-golden.jsonl

One JSON object per line:

```json
{"id": "...", "note": "...", "text": "...",
 "expected": [{"kind": "ref_no", "raw_text": "...", "start": 66, "end": 88,
               "groups": {"ref_no": "...", "sheet_no": "..."}}]}
```

Conventions both implementations must follow:

- `start` / `end` are character offsets into `text`, and `text[start:end] == raw_text`.
  `report.py` asserts this on load and prints `integrity: FAIL` if it ever stops holding.
  Every fixture text is BMP-only, so Python code-point offsets and Java UTF-16 offsets
  agree; do not add emoji or other astral characters.
- `raw_text` is the **whole** match, prefix included (`"sp. zn. 6 Ads 45/2014"`), not the
  capture group.
- `kind` is the `kind` field of the pattern in `extract/patterns.toml`, i.e. the
  `jg.models.ReferenceKind` value.
- `groups` maps the names listed under that pattern's `groups` key to the captured text.
  **A group that did not participate is omitted**, never present as null: an anaphoric
  `§ 2000 odst. 1` carries no `act_no`, and the resolver, not the extractor, supplies it.
- `expected` is sorted by `start`, ties broken by the order the patterns appear in
  `extract/patterns.toml`.
- `note` is documentation for humans. Implementations ignore it.

Coverage today: every pattern in `extract/patterns.toml` (`ref_no` with and without the
sheet number, `case_no_us` in both the panel and the plenum spelling, `case_no_gen`,
`ecli`, `journal`, `provision` with and without an explicit act), plus three negative
documents that must yield zero matches.

**Read before touching `case_no_us`.** `us-caseno-plenum` and
`negative-us-without-designator` are a pair and only make sense together. The plenum
designator `Pl.` *replaces* the Roman numeral rather than prefixing it, so the pattern is
an alternation over exactly one designator. The first entry pins that a plenary number
matches; the second pins that a bare `sp. zn. ÚS 1/09` with no designator still does not,
which is what stops the widening from costing precision. Change one and you must revisit
the other. This was a real gap until the pattern was corrected — the earlier
`(?:Pl\.\s*)?[IVX]+` form matched no plenary number in either runtime, which quietly kept
plenary derogation, and therefore the `ProvisionDerogated` red light, out of reach.

**Not yet covered**, add when real text is available: overlapping spans (the Python side
already has `drop_contained`, so the tie-break needs pinning), a citation split across a
line break, and provision anaphora resolved from a preceding act number.

### How to regrow it

1. Pick paragraphs from `decision_paragraph` in the crawled corpus, not from your head.
   **Never invent a case number, ECLI, journal number or act number** (CLAUDE.md rule 1).
   Anything that is not from crawled data must carry a `TEST-` prefix that cannot resolve.
2. Include the awkward ones on purpose: a citation the rules pass misses is worth more
   than ten it gets right.
3. Compute the offsets, do not type them. Run the paragraph through
   `jg.extract.patterns.find_references`, eyeball every span, delete the wrong ones, then
   write the file. Hand-edited offsets are the main way this fixture rots.
4. Re-run `make eval-extract`. `integrity: ok` means the file is self-consistent;
   `precision` / `recall` of `1.00` means the Python side agrees with it.
5. Run the Java parity test. Divergence between the two runtimes fails the build, which is
   the entire point of the fixture.

### The extractor contract

`report.py` looks for the first callable of `extract_references`, `find_references`,
`extract_all`, `find_all`, `extract` in `jg.extract.patterns`, then `jg.extract`, and calls
it as `f(text)`, `f(text, 1)` or `f(text, paragraph_idx=1)`, whichever it accepts. The
return value may be `jg.models.Reference` objects, dataclasses or dicts, as long as each
item exposes `kind`, `raw_text`, `start`, `end` and `groups`. If nothing is importable the
report says so and prints a fixture inventory instead of pretending to measure.

## labels.csv

`citing_ecli, cited_ecli, paragraph_idx, gold_label, note`. Lines starting with `#` are
comments. `gold_label` is one of the eight values in PLAN.md section 8.

Fifty rows, spread across all seven real labels, hand-labelled with the decision text open.
Rows carrying a `TEST-` identifier are refused by the report even when uncommented, so the
placeholder block can never turn into a fake score. A gold row whose edge is missing from
the database, or present but unclassified, is counted as `(none)`: a miss for its own
label's recall and a hit for nobody's precision.

Matching is on `(citing_ecli, cited_ecli, paragraph_idx)`. If that triple finds nothing but
the pair has exactly one stored treatment, that one is used and counted in the report line
as "matched on the pair, not the paragraph"; an ambiguous pair is left unmatched.

## What each number means

**Extraction table.** Per `kind` and overall: `tp` spans matched on
`(kind, start, end, raw_text)`, `fp` extracted but not in the fixture, `fn` in the fixture
but not extracted. `precision = tp/(tp+fp)`, `recall = tp/(tp+fn)`. Group mismatches on an
otherwise correct span are counted and listed separately, not as false positives. The
acceptance bar of PLAN.md section 7 (recall ≥ 0.80, precision ≥ 0.95) is measured on the
30-document M2 sample, not on this fixture.

**Per-label table.** `gold` is the support, the number of gold rows carrying that label.
`precision` answers "when the pipeline says NARROWED, how often is it right", `recall`
answers "of the genuinely narrowed edges, how many did it find". The three cluster labels
are marked `*`.

**Confusion matrix.** Rows are gold, columns are predicted, `(none)` is the unlabelled
column. Cells inside the DISTINGUISHED / NARROWED / DEPARTED block are marked `*`: those
three are the distinctions that structural signals cannot make, so that block is where the
reasoning model either earns its cost or does not. The summary underneath splits the
cluster's errors into confusions inside it, gold rows lost out of it, and other labels
pulled into it.

**Red-light precision, the headline.** RED means what the rules engine means by RED
(PLAN.md section 9): a `QUASHED` label, or a `DEPARTED` label from a body that may
actually depart, i.e. `departure_authority.binding` is true for
`(citing_court, citing_panel, cited_court)`. A `DEPARTED` from a body that may not depart
is an amber conflict and is deliberately not counted here. Precision is
`red_tp / (red_tp + red_fp)`: of the red verdicts shown to a user, how many were real.
Red recall is printed underneath and is *expected to be lower*: per D8 a false red destroys
trust permanently while a false amber costs thirty seconds, so recall is what we spend to
buy precision. Say that number out loud in the presentation, together with the limitations
slide.
