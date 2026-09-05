import type { CheckRequest, ClaimRequest, DocumentReport, PropositionVerdict } from './types';
import { MOCK_REPORT, mockProposition } from './mock';

/** Dev server proxies /api to http://localhost:8080 (vite.config.ts). */
const CHECK_URL = '/api/documents/check';

export async function checkDocument(text: string, useMock: boolean): Promise<DocumentReport> {
  if (useMock) {
    await new Promise((resolve) => setTimeout(resolve, 200));
    return MOCK_REPORT;
  }

  const body: CheckRequest = { text };
  let response: Response;
  try {
    response = await fetch(CHECK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(
      'API na /api neodpovídá. Spusťte ji příkazem `make api`, nebo zapněte ukázková data.',
    );
  }

  if (!response.ok) {
    throw new Error(`API odpovědělo stavem ${response.status}. Kontrolu nelze provést.`);
  }
  return (await response.json()) as DocumentReport;
}

/**
 * The proposition check (M7). Unlike the document check this costs a live model call, so it
 * is triggered per source by the user rather than run for the whole document.
 *
 * A 503 is its own message: the model being unreachable is not a verdict, and must never be
 * shown as one. A 200 carrying `UNCLASSIFIED` is a normal result and comes back as data.
 */
export async function checkProposition(
  ecli: string,
  claim: string,
  useMock: boolean,
): Promise<PropositionVerdict> {
  if (useMock) {
    await new Promise((resolve) => setTimeout(resolve, 400));
    return mockProposition(claim);
  }

  const body: ClaimRequest = { claim };
  let response: Response;
  try {
    response = await fetch(`/api/decisions/${encodeURIComponent(ecli)}/proposition-check`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error('API na /api neodpovídá. Ověření tvrzení vyžaduje běžící API.');
  }

  if (response.status === 503) {
    throw new Error(
      'Jazykový model není dostupný, tvrzení proto nebylo posouzeno. ' +
        'Zkuste to znovu později — žádný závěr z toho neplyne.',
    );
  }
  if (response.status === 404) {
    throw new Error('Toto rozhodnutí není v korpusu, nelze tedy porovnat jeho text s tvrzením.');
  }
  if (!response.ok) {
    throw new Error(`API odpovědělo stavem ${response.status}. Tvrzení nebylo posouzeno.`);
  }
  return (await response.json()) as PropositionVerdict;
}
