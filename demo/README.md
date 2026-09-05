# Demo documents

Paste-or-drop fixtures for the web page. Every case number and act number in them is real,
read out of the crawled corpus — nothing here is invented (CLAUDE.md rule 1). Each was
verified against the live API before being written down, so the lights below are what the
system actually returns, not what it ought to return.

Both files depend on the corpus that is loaded: 21,212 decisions (NSS 2020-01 → 2024-04,
plus the ÚS decisions that corpus cites) with treatments and provision versions in place.
Against an empty database every row would land in *Nepřiřazené odkazy* instead, which is
correct behaviour but does not demo anything.

## `01-prekonano-a-zuzeno.txt` — the case-law lights

| Source | Light | Why |
|---|---|---|
| `č. j. 3 As 131/2021-86` | **RED** *překonáno* | `SUPERSEDED` by `ECLI:CZ:NSS:2024:2.As.103.2023.47`, a **rozšířený senát**. Two independent paragraphs (10 and 41) carry the evidence. |
| `č. j. 2 As 21/2020-34` | **AMBER** *zúženo* | `NARROWED` by **two different** decisions — `4.As.16.2023.39` and `2.As.134.2022.59`. Independent corroboration, not one model call repeated. |
| `č. j. 6 Ads 45/2014-32` | *unresolved* | 2015, outside the crawled window. Lands in *Nepřiřazené odkazy* — "Netvrdíme o nich nic." |

What to point at when showing it:

- Expand the red row. The span is the citing court's own sentence, and
  `Rozhodovací těleso: rozšířený senát` is *why* it was allowed to depart — that comes from
  the `departure_authority` table, not from the model.
- Every ECLI in the panel is a link to the court's own page (`Citující rozhodnutí`,
  `Přiřazeno k`, `Oslabený zdroj v řetězci`). It opens the decision the span was quoted
  from, so the audience can check the quotation against the source rather than against us.
  The address is the one the crawler fetched, so a decision the corpus does not hold has no
  link at all instead of a guessed one.
- The amber row has two reasons from two different decisions. That is the difference between
  a system that quotes evidence and one that asserts a conclusion.
- The unresolved row is the honest half. The corpus has holes and the page says so instead
  of returning green.

## `02-zmena-predpisu.txt` — the differentiator

| Source | Light | Why |
|---|---|---|
| `č. j. 8 Azs 342/2021-74` | **AMBER** *argumentačně oslabeno* | `PROVISION_REWORDED`. The decision has a **spotless citation history** — nothing was ever said against it. |
| `§ 12 zákona č. 325/1999 Sb.` | GREEN | The provision itself is fine; it is the decision *relying* on the old wording that is weakened. |

This is the one worth spending time on. That decision from January 2023 interprets § 12 of
the asylum act. On 2026-06-12 the substantive test in § 12 moved from the domestic
*"odůvodněný strach z pronásledování z důvodu rasy, pohlaví…"* to *"způsobilost pro
postavení uprchlíka v souladu s kapitolami II a III kvalifikačního nařízení"* — the EU Pact
on Migration and Asylum. The decision now interprets text that no longer exists.

**No citation-based system can find this**, because nothing was ever said against the
decision. It took 19 model calls to settle 938 affected decisions, because the materiality
judgement is cached on the *version pair* rather than on the decision.

## Running it

```
make api      # backend on :8080
make web      # dev server on :5173, proxies /api to :8080
```

Two terminals; `make web` installs dependencies on first run. Then open
http://localhost:5173 and drop a file onto the page, or paste its text.

`make up` first if Postgres is not already running.
