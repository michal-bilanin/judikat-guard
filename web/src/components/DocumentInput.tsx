import { useRef, useState } from 'react';

type Props = {
  text: string;
  onTextChange: (text: string) => void;
  onSubmit: () => void;
  pending: boolean;
};

const TEXT_EXTENSIONS = ['.txt', '.md', '.text'];

function isPlainText(file: File): boolean {
  if (file.type.startsWith('text/')) return true;
  const name = file.name.toLowerCase();
  return TEXT_EXTENSIONS.some((ext) => name.endsWith(ext));
}

/** Textarea plus a drop zone for a plain-text document. */
export function DocumentInput({ text, onTextChange, onSubmit, pending }: Props) {
  const [dragging, setDragging] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function readFile(file: File | undefined) {
    if (!file) return;
    if (!isPlainText(file)) {
      setFileError(
        `Soubor „${file.name}“ nelze načíst. Podporován je zatím jen prostý text (.txt, .md); ` +
          'obsah PDF nebo DOCX vložte do pole níže.',
      );
      return;
    }
    setFileError(null);
    onTextChange(await file.text());
  }

  return (
    <section className="input" aria-label="Vstupní dokument">
      <div
        className={dragging ? 'input__drop input__drop--active' : 'input__drop'}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          void readFile(event.dataTransfer.files[0]);
        }}
      >
        <textarea
          className="input__text"
          value={text}
          spellCheck={false}
          onChange={(event) => onTextChange(event.target.value)}
          placeholder="Vložte text podání, rozhodnutí nebo stanoviska. Soubor s prostým textem lze také přetáhnout sem."
          aria-label="Text dokumentu"
        />
      </div>

      <div className="input__actions">
        <button className="button button--primary" onClick={onSubmit} disabled={pending}>
          {pending ? 'Kontroluji…' : 'Zkontrolovat'}
        </button>
        <button className="button" onClick={() => fileInput.current?.click()} disabled={pending}>
          Vybrat soubor
        </button>
        <button
          className="button button--quiet"
          onClick={() => {
            onTextChange('');
            setFileError(null);
          }}
          disabled={pending || text.length === 0}
        >
          Vymazat
        </button>
        <span className="input__count">{text.length.toLocaleString('cs-CZ')} znaků</span>
        <input
          ref={fileInput}
          type="file"
          accept=".txt,.md,.text,text/plain,text/markdown"
          hidden
          onChange={(event) => {
            void readFile(event.target.files?.[0]);
            event.target.value = '';
          }}
        />
      </div>

      {fileError && <p className="input__error">{fileError}</p>}
    </section>
  );
}
