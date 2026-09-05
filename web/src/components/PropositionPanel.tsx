import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';

import { checkProposition } from '../api';
import type { PropositionVerdict } from '../types';
import {
  PROPOSITION_CAVEAT,
  PROPOSITION_HELP,
  propositionTone,
  propositionVerdict,
} from '../czech';

/** Mirrors the `@Size(max = 4_000)` on ClaimRequest, so the API never rejects on length. */
const MAX_CLAIM = 4_000;

/**
 * M7, the proposition check (PLAN.md section 12). Not "is this source current" but "are you
 * using it correctly".
 *
 * <p>Per source, and only on request. Every other verdict on this page is a read of a graph
 * the pipeline already classified; this one costs a live model call against the full text of
 * the cited decision, so the user asks for it one source at a time rather than paying for
 * the whole document up front.
 *
 * <p>The claim is typed by the user and not extracted from their document. Guessing which
 * sentence a citation was offered for is a second inference problem, and getting it wrong
 * would mean confidently judging a claim the lawyer never made.
 */
export function PropositionPanel({ ecli, useMock }: { ecli: string; useMock: boolean }) {
  const [claim, setClaim] = useState('');
  const [open, setOpen] = useState(false);

  const check = useMutation<PropositionVerdict, Error, string>({
    mutationFn: (value) => checkProposition(ecli, value, useMock),
  });

  const trimmed = claim.trim();
  const verdict = check.data;

  if (!open) {
    return (
      <button type="button" className="prop__open" onClick={() => setOpen(true)}>
        Ověřit tvrzení, pro které tento zdroj uvádíte
      </button>
    );
  }

  return (
    <section className="prop" aria-label="Ověření tvrzení proti textu rozhodnutí">
      <h3 className="prop__title">Ověřit tvrzení</h3>
      <p className="prop__help">{PROPOSITION_HELP}</p>

      <textarea
        className="prop__input"
        value={claim}
        maxLength={MAX_CLAIM}
        rows={3}
        placeholder="Např.: K prokázání zastoupení postačí prohlášení zástupce."
        onChange={(event) => setClaim(event.target.value)}
      />

      <div className="prop__actions">
        <button
          type="button"
          className="button button--primary"
          disabled={trimmed.length === 0 || check.isPending}
          onClick={() => check.mutate(trimmed)}
        >
          {check.isPending ? 'Porovnávám…' : 'Ověřit'}
        </button>
        <span className="prop__count">
          {check.isPending
            ? 'Porovnávám tvrzení s textem rozhodnutí.'
            : `${trimmed.length} / ${MAX_CLAIM} znaků`}
        </span>
      </div>

      {check.isError && (
        <p className="prop__error" role="alert">
          {check.error.message}
        </p>
      )}

      {verdict && (
        <article className={`prop__result prop__result--${propositionTone(verdict.verdict)}`}>
          <header className="prop__resultHead">
            <span className="prop__verdict">{propositionVerdict(verdict.verdict)}</span>
            <code className="prop__kind" title="Interní označení verdiktu">
              {verdict.verdict}
            </code>
          </header>

          <p className="prop__note">{verdict.note}</p>

          {/* No span means UNCLASSIFIED: the model answered and the answer could not be
              validated. Rendering an empty quote box would imply evidence exists. */}
          {verdict.evidenceSpan ? (
            <>
              <blockquote className="prop__span">{verdict.evidenceSpan}</blockquote>
              <p className="prop__meta">
                Doslovný úryvek z rozhodnutí. Jistota modelu:{' '}
                {(verdict.confidence * 100).toFixed(0)} %.
              </p>
            </>
          ) : (
            <p className="prop__meta">
              Model nevrátil doslovný úryvek, na kterém by závěr stál. Bez něj se verdikt
              nezapisuje ani nezobrazuje jako odpověď — posuďte tvrzení sami.
            </p>
          )}

          <p className="prop__caveat">{PROPOSITION_CAVEAT}</p>
        </article>
      )}
    </section>
  );
}
