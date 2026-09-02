---
version: provision-materiality.v1
cache_key: (from_version_id, to_version_id, prompt_version)
temperature: 0
placeholders:
  - act_no
  - section
  - subsec
  - from_valid_from
  - to_valid_from
  - from_body
  - to_body
---

<!--
FROZEN. Rows in `provision_materiality` reference this version string. Do not edit.
See CLAUDE.md rule 8.
-->

A statutory provision was reworded. Decide whether the change could affect legal reasoning
that relied on the earlier wording, or whether it is cosmetic.

## Provision

§ {{section}} {{subsec}} zákona č. {{act_no}} Sb.

## Wording in force from {{from_valid_from}}

```
{{from_body}}
```

## Wording in force from {{to_valid_from}}

```
{{to_body}}
```

## What counts as material

Material (`true`):

- A condition, deadline, threshold or amount changed.
- The set of persons or situations the rule covers changed.
- A discretionary power became mandatory, or the reverse.
- An exception was added or removed.
- A cross-reference now points at a rule with different content.

Not material (`false`):

- Renumbering, or a cross-reference updated to track renumbering elsewhere.
- Terminology alignment that does not change meaning, for example a defined term replaced
  by its own definition, or vocabulary harmonised with a recodification.
- Punctuation, orthography, or purely grammatical rewriting.
- Splitting one sentence into two without changing what they require.

When the change is genuinely ambiguous, answer `true`. An unnecessary amber costs the user
thirty seconds; a missed material change is the failure mode this whole check exists to catch.

## Output

Return one JSON object and nothing else.

```json
{
  "material": true,
  "confidence": 0.9,
  "evidence_span": "verbatim substring of one of the two wordings above",
  "reasoning": "one sentence"
}
```

`evidence_span` must be copied character for character from either the earlier or the later
wording — whichever one contains the text that carries your answer. It is checked
mechanically against both. Quote the changed clause, not the whole provision.
