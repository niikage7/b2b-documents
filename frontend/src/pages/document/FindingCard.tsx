import { useQuery } from '@tanstack/react-query';
import { Check, ChevronDown, FileSearch, History, Pencil, Trash2, Undo2, X } from 'lucide-react';
import { forwardRef, useRef, useState } from 'react';
import { Link } from 'react-router';
import { api } from '../../api/endpoints';
import { keys } from '../../api/queries';
import type { Finding, FindingPatch, Level } from '../../api/types';
import { Dialog, errorMessage } from '../../components/feedback';
import { Button, Confidence, LevelBadge, Segmented, Spinner } from '../../components/ui';
import { formatWhen } from '../../lib/format';
import { FINDING_STATUS_LABELS, HISTORY_ACTION_LABELS, LEVEL_LABELS } from '../../lib/labels';
import { refLink, refPlace } from '../BatchAssist';

export type CardMode = 'compact' | 'summary' | 'expanded';

export function sectionLabel(f: Pick<Finding, 'section_number' | 'section_title' | 'page'>): string {
  const parts: string[] = [];
  if (f.section_number) parts.push(`п. ${f.section_number}${f.section_title ? ` «${f.section_title}»` : ''}`);
  else if (f.section_title) parts.push(`раздел «${f.section_title}»`);
  if (f.page) parts.push(`стр. ${f.page}`);
  return parts.join(', ') || 'раздел не определён';
}

function shortSection(f: Finding): string {
  return [f.section_number && `п. ${f.section_number}`, f.page && `стр. ${f.page}`].filter(Boolean).join(' · ');
}

function title(f: Finding) {
  return f.rule?.title ?? 'Ручная пометка';
}

function StatusMark({ f }: { f: Finding }) {
  if (f.status === 'accepted' && f.source === 'ai') {
    return (
      <span className="fc__status fc__status--accepted">
        <Check size={13} /> Принято
      </span>
    );
  }
  if (f.status === 'rejected') return <span className="fc__status fc__status--rejected">Отклонено</span>;
  if (f.status === 'edited') return <span className="fc__status fc__status--edited">Изменено</span>;
  if (f.source === 'user') return <span className="fc__status">Ручная</span>;
  return null;
}

function OriginalDialog({ finding, onClose }: { finding: Finding; onClose: () => void }) {
  const q = useQuery({ queryKey: keys.original(finding.id), queryFn: () => api.findingOriginal(finding.id) });
  const first = useRef<HTMLSpanElement>(null);
  return (
    <Dialog
      title={q.data ? `Оригинал · стр. ${q.data.page}` : 'Оригинал'}
      width={860}
      onClose={onClose}
      actions={<Button onClick={onClose}>Закрыть</Button>}
    >
      {q.isPending && <div className="muted row"><Spinner /> Загружаем страницу…</div>}
      {q.isError && <div className="text-critical">Не удалось загрузить страницу: {errorMessage(q.error)}</div>}
      {q.data && (
        <>
          {q.data.rects.length === 0 && (
            <p className="fc__note">Точное место цитаты неизвестно: найдите её на странице по тексту.</p>
          )}
          <div className="original">
            <div className="original__page" style={{ aspectRatio: q.data.aspect }}>
              <img src={q.data.image_url} alt={`Страница ${q.data.page} исходного документа`} onLoad={() => first.current?.scrollIntoView({ block: 'center' })} />
              {q.data.rects.map((r, i) => (
                <span
                  key={i}
                  ref={i === 0 ? first : undefined}
                  className={`original__mark original__mark--${finding.level}`}
                  style={{
                    left: `${r.x0 * 100}%`,
                    top: `${r.y0 * 100}%`,
                    width: `${(r.x1 - r.x0) * 100}%`,
                    height: `${(r.y1 - r.y0) * 100}%`,
                  }}
                />
              ))}
            </div>
          </div>
        </>
      )}
    </Dialog>
  );
}

function HistoryList({ findingId }: { findingId: string }) {
  const q = useQuery({ queryKey: keys.history(findingId), queryFn: () => api.findingHistory(findingId) });
  if (q.isPending) return <div className="fc__history muted row"><Spinner /> Загружаем историю…</div>;
  if (q.isError) return <div className="fc__history text-critical">Не удалось загрузить историю</div>;
  return (
    <ol className="fc__history">
      {q.data.events.map((e, i) => {
        const levelChanged = e.old?.level && e.new?.level && e.old.level !== e.new.level;
        return (
          <li key={i}>
            <span className="fc__history-when">{formatWhen(e.created_at)}</span>
            <span>
              <b>{e.actor.full_name}</b> {HISTORY_ACTION_LABELS[e.action] ?? e.action}
              {e.action === 'created' && e.actor.full_name === 'ИИ' && ' находку'}
              {levelChanged && ` · уровень: ${LEVEL_LABELS[e.old!.level as Level]} → ${LEVEL_LABELS[e.new!.level as Level]}`}
              {e.action === 'commented' && e.new?.comment && ` · «${e.new.comment}»`}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

interface FindingCardProps {
  finding: Finding;
  mode: CardMode;
  hovered: boolean;
  position: { index: number; total: number } | null;
  onActivate: () => void;
  onClose: () => void;
  onHover: (hovered: boolean) => void;
  onSection: () => void;
  onPatch: (patch: Omit<FindingPatch, 'version'>, opts?: { undoable?: boolean }) => Promise<void>;
  onDelete: () => void;
  style?: React.CSSProperties;
}

export const FindingCard = forwardRef<HTMLDivElement, FindingCardProps>(function FindingCard(
  { finding: f, mode, hovered, position, onActivate, onClose, onHover, onSection, onPatch, onDelete, style },
  ref,
) {
  const [editing, setEditing] = useState(false);
  const [commenting, setCommenting] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);
  const [level, setLevel] = useState<Level>(f.level);
  const [comment, setComment] = useState(f.comment ?? '');
  const [busy, setBusy] = useState<string | null>(null);

  const run = async (key: string, patch: Omit<FindingPatch, 'version'>, opts?: { undoable?: boolean }) => {
    setBusy(key);
    try {
      await onPatch(patch, opts);
      return true;
    } catch {
      return false;
    } finally {
      setBusy(null);
    }
  };

  const classes = ['fc', `fc--${f.level}`, `fc--${mode}`, `fc--status-${f.status}`, hovered && 'fc--hover'].filter(Boolean).join(' ');
  const common = {
    ref,
    style,
    'data-finding-id': f.id,
    onMouseEnter: () => onHover(true),
    onMouseLeave: () => onHover(false),
  };

  if (mode === 'compact') {
    return (
      <div {...common} className={classes}>
        <button type="button" className="fc__compact" onClick={onActivate} aria-label={`${LEVEL_LABELS[f.level]}: ${title(f)}`}>
          <LevelBadge level={f.level} small />
          <span className="fc__compact-title">
            {f.rule ? `${f.rule.code} · ` : ''}
            {title(f)}
          </span>
          <ChevronDown size={16} />
        </button>
      </div>
    );
  }

  if (mode === 'summary') {
    return (
      <div {...common} className={classes}>
        <button type="button" className="fc__summary" onClick={onActivate}>
          <span className="fc__head">
            <LevelBadge level={f.level} small />
            {f.rule && <span className="mono subtle">{f.rule.code}</span>}
            <StatusMark f={f} />
          </span>
          <span className="fc__title">{title(f)}</span>
          <span className="fc__meta">
            {shortSection(f)}
            {(f.decided_by || f.author) && f.status !== 'new' && ` · ${(f.decided_by ?? f.author)!.full_name}${formatWhen(f.decided_at ?? f.created_at) ? `, ${formatWhen(f.decided_at ?? f.created_at)}` : ''}`}
          </span>
        </button>
      </div>
    );
  }

  const decided = f.status !== 'new' && f.decided_by;
  const isUser = f.source === 'user';
  return (
    <div {...common} className={classes} role="region" aria-label={`Находка: ${title(f)}`}>
      <div className="fc__head">
        <LevelBadge level={f.level} small />
        {f.rule && <span className="mono subtle">{f.rule.code}</span>}
        {position && (
          <span className="subtle">
            {position.index} из {position.total}
          </span>
        )}
        <StatusMark f={f} />
        <span className="spacer" />
        <button type="button" className="icon-btn" aria-label="Свернуть выноску" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <div className="fc__title fc__title--lg">{title(f)}</div>
      <button type="button" className="btn btn--link fc__section" onClick={onSection}>
        {sectionLabel(f)}
      </button>

      {!isUser && f.explanation && <p className="fc__text">{f.explanation}</p>}
      {!isUser && f.recommendation && (
        <p className="fc__text fc__recommendation">
          <b>Что сделать:</b> {f.recommendation}
        </p>
      )}
      {f.related && f.related.length > 0 && (
        <div className="fc__related">
          <b>Другое значение в пакете:</b>
          {f.related.map((ref, i) => (
            <Link key={i} to={refLink(ref)}>
              {ref.value ? `${ref.value} — ` : `«${ref.quote}» — `}
              {refPlace(ref)}
            </Link>
          ))}
        </div>
      )}
      {f.source_available && (
        <button type="button" className="btn btn--link fc__original" onClick={() => setShowOriginal(true)}>
          <FileSearch size={14} /> Сверить с оригиналом
        </button>
      )}
      {showOriginal && <OriginalDialog finding={f} onClose={() => setShowOriginal(false)} />}
      {f.from_scan && <p className="fc__note">Текст распознан со скана: сверьте значения с оригиналом.</p>}
      {f.status === 'edited' && f.original_level !== f.level && (
        <p className="fc__note">Уровень изменён: было «{LEVEL_LABELS[f.original_level]}»</p>
      )}

      <div className="fc__source">
        {isUser ? (
          <span>
            Ручная пометка · {f.author?.full_name ?? 'автор неизвестен'}
            {f.created_at && `, ${formatWhen(f.created_at)}`}
          </span>
        ) : (
          <>
            <span>Найдено ИИ</span>
            {f.confidence_level && (
              <>
                <span aria-hidden>·</span>
                <Confidence level={f.confidence_level} />
              </>
            )}
          </>
        )}
      </div>
      {decided && !isUser && (
        <div className="fc__decided">
          {FINDING_STATUS_LABELS[f.status]} · {f.decided_by!.full_name}, {formatWhen(f.decided_at)}
        </div>
      )}

      {f.comment && !commenting && !editing && (
        <div className="fc__comment">
          <b>Комментарий:</b> {f.comment}
        </div>
      )}

      {editing ? (
        <form
          className="fc__form"
          onSubmit={async (e) => {
            e.preventDefault();
            const patch: Omit<FindingPatch, 'version'> = {};
            if (level !== f.level) patch.level = level;
            if (comment.trim() !== (f.comment ?? '')) {
              patch.comment = comment.trim();
              if (isUser) patch.explanation = comment.trim();
            }
            if (!Object.keys(patch).length || (await run('edit', patch))) setEditing(false);
          }}
        >
          <Segmented
            label="Уровень находки"
            small
            value={level}
            onChange={setLevel}
            options={[
              { value: 'critical', label: <><span className="dot dot--critical" /> Критично</> },
              { value: 'warning', label: <><span className="dot dot--warning" /> Внимание</> },
            ]}
          />
          <textarea
            className="textarea"
            rows={3}
            placeholder="Комментарий юриста"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            aria-label="Комментарий"
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="row">
            <Button size="sm" variant="primary" type="submit" loading={busy === 'edit'}>
              Сохранить
            </Button>
            <Button size="sm" onClick={() => setEditing(false)}>
              Отмена
            </Button>
          </div>
        </form>
      ) : commenting ? (
        <form
          className="fc__form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await run('comment', { comment: comment.trim() })) setCommenting(false);
          }}
        >
          <textarea
            className="textarea"
            rows={3}
            autoFocus
            placeholder="Например: направить запрос на разъяснение"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            aria-label="Комментарий"
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="row">
            <Button size="sm" variant="primary" type="submit" loading={busy === 'comment'}>
              Сохранить
            </Button>
            <Button size="sm" onClick={() => { setCommenting(false); setComment(f.comment ?? ''); }}>
              Отмена
            </Button>
          </div>
        </form>
      ) : (
        <div className="fc__actions">
          {isUser ? (
            <>
              <Button size="sm" onClick={() => { setLevel(f.level); setComment(f.comment ?? ''); setEditing(true); }}>
                <Pencil size={14} /> Изменить
              </Button>
              {f.can_delete && (
                <Button size="sm" variant="danger" onClick={onDelete}>
                  <Trash2 size={14} /> Удалить
                </Button>
              )}
            </>
          ) : f.status === 'new' || f.status === 'edited' ? (
            <>
              <Button size="sm" variant="dark" loading={busy === 'accept'} onClick={() => void run('accept', { status: 'accepted' })}>
                Принять
              </Button>
              <Button size="sm" loading={busy === 'reject'} onClick={() => void run('reject', { status: 'rejected' }, { undoable: true })}>
                Отклонить
              </Button>
              <Button size="sm" onClick={() => { setLevel(f.level); setComment(f.comment ?? ''); setEditing(true); }}>
                Изменить
              </Button>
              {f.status === 'edited' && (
                <Button size="sm" variant="ghost" loading={busy === 'reopen'} onClick={() => void run('reopen', { status: 'new' })}>
                  <Undo2 size={14} /> Отменить изменения
                </Button>
              )}
            </>
          ) : (
            <Button size="sm" loading={busy === 'reopen'} onClick={() => void run('reopen', { status: 'new' })}>
              <Undo2 size={14} /> Отменить решение
            </Button>
          )}
        </div>
      )}

      {!editing && !commenting && (
        <div className="fc__links">
          {!isUser && (
            <button type="button" className="btn btn--link" onClick={() => { setComment(f.comment ?? ''); setCommenting(true); }}>
              {f.comment ? 'Изменить комментарий' : 'Добавить комментарий'}
            </button>
          )}
          <button type="button" className="btn btn--link fc__history-toggle" aria-expanded={showHistory} onClick={() => setShowHistory((v) => !v)}>
            <History size={14} /> История
          </button>
        </div>
      )}
      {showHistory && <HistoryList findingId={f.id} />}
    </div>
  );
});
