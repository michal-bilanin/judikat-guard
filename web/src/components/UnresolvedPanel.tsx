import type { CorpusCoverage, UnresolvedRef } from '../types';
import { corpusScope, formatCount, formatDate, unresolvedReason } from '../czech';

type Props = {
  unresolved: UnresolvedRef[];
  corpus: Record<string, CorpusCoverage>;
};

/**
 * Unresolved references are a first-class part of the answer, not an error (PLAN.md
 * section 11). Being explicit about the coverage gap is the product.
 */
export function UnresolvedPanel({ unresolved, corpus }: Props) {
  const scope = corpusScope(corpus);

  return (
    <section className="panel panel--unresolved" aria-label="Nepřiřazené odkazy">
      <header className="panel__head">
        <h2>Nepřiřazené odkazy</h2>
        <p className="panel__counts">celkem {unresolved.length}</p>
      </header>

      <p className="panel__caveat">
        Tyto odkazy jsme v dokumentu našli, ale nepodařilo se je spojit s rozhodnutím ani
        s ustanovením v korpusu {formatCount(scope.total)} rozhodnutí zveřejněných do{' '}
        {formatDate(scope.through)}. Netvrdíme o nich nic — nedostaly zelené ani červené světlo.
        Zkontrolujte je ručně.
      </p>

      {unresolved.length === 0 ? (
        <p className="panel__empty">Všechny nalezené odkazy se podařilo přiřadit.</p>
      ) : (
        <ul className="unresolved">
          {unresolved.map((ref, idx) => (
            <li key={`${ref.rawText}-${idx}`} className="unresolved__item">
              <span className="unresolved__raw">{ref.rawText}</span>
              <span className="unresolved__reason">{unresolvedReason(ref.reason)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
