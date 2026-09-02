import type { CheckRequest, DocumentReport } from './types';
import { MOCK_REPORT } from './mock';

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
