import { ChevronLeft, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import type { BatchView, Level } from '../../api/types';
import { Button, LevelDot, Segmented } from '../../components/ui';

function summary(doc: BatchView['documents'][number]) {
  if (doc.status === 'error') return <span className="text-critical">Ошибка обработки</span>;
  if (doc.status !== 'done') return <span className="muted">{doc.stage_label}</span>;
  const parts = [];
  if (doc.findings.critical) parts.push(`Критично ${doc.findings.critical}`);
  if (doc.findings.warning) parts.push(`Внимание ${doc.findings.warning}`);
  return <span className="muted">{parts.length ? parts.join(' · ') : 'Без рисков'}</span>;
}

/** Документы пакета слева: переключение без возврата на «Разбор пакета». Панель сворачивается. */
export function DocumentsPanel({ batch, currentId, collapsed, onToggle }: { batch: BatchView | undefined; currentId: string; collapsed: boolean; onToggle: () => void }) {
  if (collapsed) {
    return (
      <aside className="docs-panel docs-panel--collapsed">
        <button type="button" className="icon-btn" onClick={onToggle} aria-label="Показать документы пакета" title="Документы пакета">
          <PanelLeftOpen size={18} />
        </button>
        {batch?.documents.map((d) => (
          <Link key={d.id} to={`/documents/${d.id}`} className={d.id === currentId ? 'docs-panel__dot docs-panel__dot--current' : 'docs-panel__dot'} title={d.filename}>
            <LevelDot level={d.worst_level} />
          </Link>
        ))}
      </aside>
    );
  }
  return (
    <aside className="docs-panel">
      <div className="docs-panel__top">
        {batch ? (
          <Link to={`/batches/${batch.id}`} className="docs-panel__back">
            <ChevronLeft size={16} /> {batch.name}
          </Link>
        ) : (
          <span />
        )}
        <button type="button" className="icon-btn" onClick={onToggle} aria-label="Свернуть панель документов" title="Свернуть">
          <PanelLeftClose size={18} />
        </button>
      </div>
      <div className="docs-panel__caption">Документы пакета</div>
      <nav className="docs-panel__list" aria-label="Документы пакета">
        {batch?.documents.map((d) => (
          <Link key={d.id} to={`/documents/${d.id}`} className={d.id === currentId ? 'docs-panel__item docs-panel__item--current' : 'docs-panel__item'} aria-current={d.id === currentId ? 'page' : undefined}>
            <LevelDot level={d.worst_level} />
            <span className="docs-panel__text">
              <span className="docs-panel__name">{d.filename}</span>
              <span className="docs-panel__summary">{summary(d)}</span>
            </span>
          </Link>
        ))}
      </nav>
    </aside>
  );
}

/** Меню у выделенного текста: уровень, комментарий, «Добавить» и «Отмена». */
export function NewMarkPopover({
  top,
  left,
  quote,
  onAdd,
  onCancel,
}: {
  top: number;
  left: number;
  quote: string;
  onAdd: (level: Level, comment: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [level, setLevel] = useState<Level>('warning');
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLFormElement>(null);

  useEffect(() => {
    ref.current?.querySelector('textarea')?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onCancel();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  return (
    <form
      ref={ref}
      className="mark-popover"
      style={{ top, left }}
      onMouseUp={(e) => e.stopPropagation()}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await onAdd(level, comment.trim());
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="mark-popover__title">Новая пометка</div>
      <div className="mark-popover__quote">«{quote.length > 90 ? `${quote.slice(0, 90)}…` : quote}»</div>
      <Segmented
        label="Уровень пометки"
        value={level}
        onChange={setLevel}
        options={[
          { value: 'critical', label: <><span className="dot dot--critical" /> Критично</> },
          { value: 'warning', label: <><span className="dot dot--warning" /> Внимание</> },
        ]}
      />
      <label className="field">
        <span className="field__label">Комментарий</span>
        <textarea
          className="textarea"
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Например: отсрочка оплаты 30 дней — заложить в цену"
          onKeyDown={(e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
      </label>
      <div className="row" style={{ justifyContent: 'flex-end' }}>
        <Button size="sm" onClick={onCancel}>
          Отмена
        </Button>
        <Button size="sm" variant="primary" type="submit" loading={busy}>
          Добавить
        </Button>
      </div>
    </form>
  );
}
