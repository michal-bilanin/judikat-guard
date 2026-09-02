import type { CorpusCoverage } from '../types';
import {
  SCOPE_CAVEAT,
  corpusScope,
  formatCount,
  formatDate,
  scopeSentence,
} from '../czech';

type Props = {
  asOf: string;
  corpus: Record<string, CorpusCoverage>;
};

/**
 * States the scope of everything below it (PLAN.md section 2). This line is the reason
 * the tool is defensible in front of lawyers, so it sits above the results, not in a
 * footnote.
 */
export function ScopeHeader({ asOf, corpus }: Props) {
  const scope = corpusScope(corpus);
  const courts = Object.entries(corpus).sort(([a], [b]) => a.localeCompare(b));

  return (
    <section className="scope" aria-label="Rozsah posouzení">
      <p className="scope__claim">{scopeSentence(asOf, scope)}</p>
      <p className="scope__caveat">{SCOPE_CAVEAT}</p>
      {courts.length > 0 && (
        <ul className="scope__courts">
          {courts.map(([court, coverage]) => (
            <li key={court}>
              <strong>{court}</strong> {formatCount(coverage.count)} rozhodnutí, zveřejněná do{' '}
              {formatDate(coverage.through)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
