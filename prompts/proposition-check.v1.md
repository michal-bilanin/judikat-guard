---
version: proposition-check.v1
cache_key: (cited_ecli, claim_hash, prompt_version)
temperature: 0
placeholders:
  - cited_ecli
  - cited_court
  - cited_date
  - cited_ratio
  - cited_context
  - claim
---

<!--
FROZEN. See CLAUDE.md rule 8. Add proposition-check.v2.md rather than editing this file.
-->

A lawyer's document cites a decision in support of a claim. Decide whether the decision
actually supports that claim.

## The cited decision

{{cited_court}}, {{cited_ecli}}, decided {{cited_date}}.

What it held:

```
{{cited_ratio}}
```

Relevant passages:

```
{{cited_context}}
```

## The claim it is offered to support

```
{{claim}}
```

## Verdicts

| Verdict | Use when |
|---|---|
| `SUPPORTS` | The decision holds what the claim says it holds. |
| `OVERBROAD` | The decision supports a narrower version of the claim. It held this only for certain facts, certain parties, or a certain kind of thing, and the claim drops that limit. |
| `UNRELATED` | The decision does not address the claim either way. |
| `CONTRADICTS` | The decision holds the opposite of the claim. |

`OVERBROAD` is the common and useful case: the citation is real and the reasoning is real,
but the claim reaches further than the holding does. Say precisely what the limit is.

Judge the decision as written. Do not consider whether later case law changed it — that is a
separate check, and mixing the two makes both unreadable.

## Output

Return one JSON object and nothing else.

```json
{
  "verdict": "OVERBROAD",
  "confidence": 0.78,
  "evidence_span": "verbatim substring of the holding or passages above",
  "note": "one sentence in Czech, addressed to the lawyer"
}
```

`evidence_span` must be copied character for character from the holding or the passages
above. It is checked mechanically; a response that fails the check is retried once and then
discarded.

`note` is shown to the user, so write it in Czech and in the vocabulary a Czech lawyer uses.
For `OVERBROAD`, name the limit the decision imposed, for example: *rozhodnutí tento závěr
vyslovilo pouze ve vztahu k věcem movitým*.
