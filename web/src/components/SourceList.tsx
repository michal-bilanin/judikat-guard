import { useState } from 'react';

import type { DocumentReport, Light, Reason, SourceReport } from '../types';
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

type MetaRow = { term: string; value: string; code?: boolean };

function metaRows(reason: Reason): MetaRow[] {
  const rows: MetaRow[] = [];
  if (reason.byEcli) rows.push({ term: 'Citující rozhodnutí', value: reason.byEcli, code: true });
  if (reason.viaEcli) rows.push({ term: 'Oslabený zdroj v řetězci', value: reason.viaEcli, code: true });
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

function ReasonBlock({ reason, light }: { reason: Reason; light: Light }) {
  const rows = metaRows(reason);
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
              <dd>{row.code ? <code>{row.value}</code> : row.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <p className="reason__detail">{reasonDetail(reason.kind)}</p>
    </article>
  );
}

function SourceItem({ source, report }: { source: SourceReport; report: DocumentReport }) {
  const [open, setOpen] = useState(false);
  const phrase = verdictPhrase(source.light, source.reasons);
  const scope = corpusScope(report.corpus);

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
              Přiřazeno k <code>{source.ecli}</code>
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
              />
            ))
          )}
        </div>
      )}
    </li>
  );
}

export function SourceList({ report }: { report: DocumentReport }) {
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
            <SourceItem key={`${source.ecli ?? source.rawText}-${idx}`} source={source} report={report} />
          ))}
        </ul>
      )}
    </section>
  );
}
