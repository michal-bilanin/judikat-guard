import { useState } from 'react';

import type { DocumentReport, Light, Reason, SourceReport } from '../types';
import { PropositionPanel } from './PropositionPanel';
import {
  LIGHT_NAME,
  corpusScope,
  formatCount,
  formatDate,
  panelName,
  reasonDetail,
  reasonHeadline,
  sourceScopeSentence,
  verdictPhrase,
} from '../czech';

type MetaRow = { term: string; value: string; code?: boolean; href?: string };

/**
 * The evidence panel quotes a span and names the decision it came from. Until the reader
 * can open that decision, they are taking our word for it — so wherever the report carries
 * a link for an ECLI, the ECLI becomes one. A missing entry renders as plain text: no
 * constructed URLs, because a link that guesses where a decision lives is worse than none.
 */
function metaRows(reason: Reason, links: Record<string, string>): MetaRow[] {
  const rows: MetaRow[] = [];
  if (reason.byEcli) {
    rows.push({
      term: 'Citující rozhodnutí',
      value: reason.byEcli,
      code: true,
      href: links[reason.byEcli],
    });
  }
  if (reason.viaEcli) {
    rows.push({
      term: 'Oslabený zdroj v řetězci',
      value: reason.viaEcli,
      code: true,
      href: links[reason.viaEcli],
    });
  }
  const panel = panelName(reason.panel);
  if (panel) rows.push({ term: 'Rozhodovací těleso', value: panel });
  if (reason.paragraph !== null && reason.paragraph !== undefined) {
    rows.push({ term: 'Odstavec', value: String(reason.paragraph) });
  }
  if (reason.provisionId !== null && reason.provisionId !== undefined) {
    rows.push({ term: 'Ustanovení (id v korpusu)', value: String(reason.provisionId), code: true });
  }
  if (reason.changedOn) rows.push({ term: 'Změna znění ke dni', value: formatDate(reason.changedOn) });
  if (reason.material !== null && reason.material !== undefined) {
    rows.push({ term: 'Povaha změny', value: reason.material ? 'věcná' : 'formální' });
  }
  return rows;
}

/**
 * A monospaced identifier, linked to the court's own page when the report knows one.
 * Rows that are not ECLIs (the provision id) never carry an href and render unchanged.
 */
function Identifier({ text, href }: { text: string; href?: string }) {
  if (!href) return <code>{text}</code>;
  return (
    <a
      className="sourceLink"
      href={href}
      target="_blank"
      // noopener because the target is a third-party court site we do not control.
      rel="noreferrer noopener"
      title="Otevřít rozhodnutí na stránkách soudu (nová karta)"
    >
      <code>{text}</code>
      <span aria-hidden="true"> ↗</span>
    </a>
  );
}

function ReasonBlock({
  reason,
  light,
  links,
}: {
  reason: Reason;
  light: Light;
  links: Record<string, string>;
}) {
  const rows = metaRows(reason, links);
  return (
    <article className="reason">
      <header className="reason__head">
        <span className={`reason__label reason__label--${light}`}>
          {reasonHeadline(reason.kind, light)}
        </span>
        <code className="reason__kind" title="Interní označení vztahu">
          {reason.kind}
        </code>
      </header>

      {reason.span ? (
        <blockquote className="reason__span">{reason.span}</blockquote>
      ) : (
        <p className="reason__nospan">
          Bez doslovné citace. Tento důvod se opírá o záznam o změně znění ustanovení, nikoli
          o text jiného rozhodnutí.
        </p>
      )}

      {rows.length > 0 && (
        <dl className="reason__meta">
          {rows.map((row) => (
            <div key={row.term} className="reason__metaRow">
              <dt>{row.term}</dt>
              <dd>
                {row.code ? <Identifier text={row.value} href={row.href} /> : row.value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      <p className="reason__detail">{reasonDetail(reason.kind)}</p>
    </article>
  );
}

function SourceItem({
  source,
  report,
  useMock,
}: {
  source: SourceReport;
  report: DocumentReport;
  useMock: boolean;
}) {
  const [open, setOpen] = useState(false);
  const phrase = verdictPhrase(source.light, source.reasons);
  const scope = corpusScope(report.corpus);
  // Tolerates an API build older than this page: no links rather than a blank report.
  const links = report.links ?? {};

  return (
    <li className={`source source--${source.light}`}>
      <button
        type="button"
        className="source__row"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className={`light light--${source.light}`} aria-hidden="true" />
        <span className="sr-only">{LIGHT_NAME[source.light]}: </span>
        {/* The citation exactly as it appeared in the document. Never reconstructed. */}
        <span className="source__raw">{source.rawText}</span>
        <span className="source__verdict">{phrase}</span>
        <span className="source__toggle" aria-hidden="true">
          {open ? '−' : '+'}
        </span>
      </button>

      {open && (
        <div className="source__evidence">
          <p className="source__scope">{sourceScopeSentence(report.asOf, scope, phrase)}</p>
          {source.ecli && (
            <p className="source__ecli">
              Přiřazeno k <Identifier text={source.ecli} href={links[source.ecli]} />
            </p>
          )}
          {source.reasons.length === 0 ? (
            <p className="source__green">
              V korpusu {formatCount(scope.total)} rozhodnutí zveřejněných do{' '}
              {formatDate(scope.through)} nebylo nalezeno žádné nepříznivé nakládání s tímto
              zdrojem. Nezveřejněná rozhodnutí korpus neobsahuje.
            </p>
          ) : (
            source.reasons.map((reason, idx) => (
              <ReasonBlock
                key={`${reason.kind}-${reason.byEcli ?? reason.viaEcli ?? idx}-${idx}`}
                reason={reason}
                light={source.light}
                links={links}
              />
            ))
          )}

          {/* Decisions only. A provision has no ECLI and holds nothing, so there is no
              holding to compare a claim against (PLAN.md section 12). */}
          {source.ecli && <PropositionPanel ecli={source.ecli} useMock={useMock} />}
        </div>
      )}
    </li>
  );
}

export function SourceList({ report, useMock }: { report: DocumentReport; useMock: boolean }) {
  const counts = report.sources.reduce<Record<Light, number>>(
    (acc, source) => ({ ...acc, [source.light]: acc[source.light] + 1 }),
    { GREEN: 0, AMBER: 0, RED: 0 },
  );

  return (
    <section className="panel" aria-label="Zdroje, o které se dokument opírá">
      <header className="panel__head">
        <h2>Zdroje v dokumentu</h2>
        <p className="panel__counts">
          celkem {report.sources.length} · červená {counts.RED} · oranžová {counts.AMBER} · zelená{' '}
          {counts.GREEN}
        </p>
      </header>

      {report.sources.length === 0 ? (
        <p className="panel__empty">
          V dokumentu nebyl rozpoznán žádný odkaz na rozhodnutí ani na ustanovení.
        </p>
      ) : (
        <ul className="sources">
          {report.sources.map((source, idx) => (
            <SourceItem
              key={`${source.ecli ?? source.rawText}-${idx}`}
              source={source}
              report={report}
              useMock={useMock}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
