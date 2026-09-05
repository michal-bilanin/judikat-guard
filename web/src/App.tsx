import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';

import { checkDocument } from './api';
import { MOCK_DOCUMENT_TEXT } from './mock';
import type { DocumentReport } from './types';
import { DocumentInput } from './components/DocumentInput';
import { ScopeHeader } from './components/ScopeHeader';
import { SourceList } from './components/SourceList';
import { UnresolvedPanel } from './components/UnresolvedPanel';

const MOCK_STORAGE_KEY = 'jg.mockData';

function readMockPreference(): boolean {
  try {
    return window.localStorage.getItem(MOCK_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeMockPreference(value: boolean): void {
  try {
    window.localStorage.setItem(MOCK_STORAGE_KEY, value ? '1' : '0');
  } catch {
    // Private mode or blocked storage; the toggle still works for this session.
  }
}

export default function App() {
  const [text, setText] = useState('');
  const [useMock, setUseMock] = useState(readMockPreference);

  const check = useMutation<DocumentReport, Error, { text: string; useMock: boolean }>({
    mutationFn: (input) => checkDocument(input.text, input.useMock),
  });

  const report = check.data;

  function toggleMock(next: boolean) {
    setUseMock(next);
    writeMockPreference(next);
    if (next && text.trim().length === 0) setText(MOCK_DOCUMENT_TEXT);
  }

  return (
    <div className="page">
      <header className="masthead">
        <div>
          <h1>Judikát Guard</h1>
          <p className="masthead__sub">
            Kontrola zdrojů, o které se dokument opírá. Ke každému verdiktu doslovná citace
            a odstavec, ze kterého pochází.
          </p>
        </div>
        <label className="masthead__mock">
          <input
            type="checkbox"
            checked={useMock}
            onChange={(event) => toggleMock(event.target.checked)}
          />
          Ukázková data (bez API)
        </label>
      </header>

      {useMock && (
        <p className="banner banner--mock">
          Zobrazují se ukázková data. Identifikátory s předponou <code>TEST-</code> neodpovídají
          žádnému skutečnému rozhodnutí.
        </p>
      )}

      <DocumentInput
        text={text}
        onTextChange={setText}
        onSubmit={() => check.mutate({ text, useMock })}
        pending={check.isPending}
      />

      {check.isError && (
        <div className="banner banner--error" role="alert">
          <p>{check.error.message}</p>
          {!useMock && (
            <button
              type="button"
              className="button"
              onClick={() => {
                toggleMock(true);
                check.mutate({ text: text.trim().length > 0 ? text : MOCK_DOCUMENT_TEXT, useMock: true });
              }}
            >
              Zobrazit ukázková data
            </button>
          )}
        </div>
      )}

      {report && (
        <main className="report">
          <ScopeHeader asOf={report.asOf} corpus={report.corpus} />
          <SourceList report={report} useMock={useMock} />
          <UnresolvedPanel unresolved={report.unresolved} corpus={report.corpus} />
        </main>
      )}
    </div>
  );
}
