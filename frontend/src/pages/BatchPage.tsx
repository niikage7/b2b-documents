import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Download, Loader, MoreHorizontal, Pencil, RotateCw, SearchX, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';
import { api } from '../api/endpoints';
import { keys, useBatch, useBatchEvents } from '../api/queries';
import type { BatchView, DocumentCard, FindingStatus, Level, PassportField } from '../api/types';
import { DeleteBatchDialog, RenameBatchDialog } from '../components/BatchDialogs';
import { ConfirmDialog, Dialog, EmptyState, ErrorState, errorMessage, ListSkeleton, Menu, useToast } from '../components/feedback';
import { Breadcrumbs, Button, Field, FileTypeIcon, FindingsBadges, LevelBadge, LevelDot, ProgressBar, Skeleton, Spinner, WorstLevelBadge } from '../components/ui';
import { formatDateTime } from '../lib/format';
import { FINDING_STATUS_LABELS } from '../lib/labels';
import { useExport } from '../lib/useExport';
import { documentMeta, isBatchRunning, lowQualityNote, useRetryDocument } from './ProcessingPage';

type DocFilter = 'all' | 'ok' | 'warning' | 'critical';
type Tab = 'documents' | 'findings';

const FINDINGS_PAGE = 20;

function matchesFilter(doc: DocumentCard, filter: DocFilter) {
  return filter === 'all' || doc.worst_level === filter;
}

export function findingLink(documentId: string, findingId: string) {
  return `/documents/${documentId}?finding=${findingId}`;
}

function CounterCards({ batch, filter, onChange }: { batch: BatchView; filter: DocFilter; onChange: (f: DocFilter) => void }) {
  const c = batch.counters ?? { total: batch.documents.length, ok: 0, warning: 0, critical: 0, errors: 0 };
  const cards: { key: DocFilter; label: string; value: number; tone?: 'ok' | 'warning' | 'critical' }[] = [
    { key: 'all', label: 'Всего документов', value: c.total },
    { key: 'ok', label: 'Без рисков', value: c.ok, tone: 'ok' },
    { key: 'warning', label: 'Внимание', value: c.warning, tone: 'warning' },
    { key: 'critical', label: 'Критично', value: c.critical, tone: 'critical' },
  ];
  return (
    <div className="counter-cards" role="group" aria-label="Фильтр документов по уровню">
      {cards.map((card) => (
        <button
          key={card.key}
          type="button"
          className={filter === card.key ? 'counter-card counter-card--active' : 'counter-card'}
          aria-pressed={filter === card.key}
          onClick={() => onChange(filter === card.key ? 'all' : card.key)}
        >
          <span className={card.tone ? `counter-card__label counter-card__label--${card.tone}` : 'counter-card__label'}>
            {card.tone && <LevelDot level={card.tone} />}
            {card.label}
          </span>
          <span className="counter-card__value">{card.value}</span>
        </button>
      ))}
    </div>
  );
}

function DocumentRow({
  doc,
  primary,
  onRetry,
  retrying,
  onDownload,
  downloading,
}: {
  doc: DocumentCard;
  primary: boolean;
  onRetry: () => void;
  retrying: boolean;
  onDownload: () => void;
  downloading: boolean;
}) {
  const lowQuality = lowQualityNote(doc.ocr_low_quality_pages);
  if (doc.status === 'error') {
    return (
      <li className="doc-card doc-card--error">
        <FileTypeIcon filename={doc.filename} kind={doc.kind} />
        <div className="doc-card__name">
          <div className="doc-card__title">{doc.filename}</div>
          <div className="text-critical doc-card__meta">Ошибка: {doc.error?.message?.replace(/^./, (ch) => ch.toLowerCase()) ?? 'не удалось обработать'}</div>
        </div>
        <WorstLevelBadge level="not_checked" small />
        <div className="doc-card__actions">
          <Button onClick={onRetry} loading={retrying}>
            <RotateCw size={16} /> Повторить
          </Button>
        </div>
      </li>
    );
  }
  if (doc.status !== 'done') {
    return (
      <li className="doc-card doc-card--processing">
        <FileTypeIcon filename={doc.filename} kind={doc.kind} />
        <div className="doc-card__name">
          <div className="doc-card__title">{doc.filename}</div>
          <div className="doc-card__meta">{documentMeta(doc)}</div>
        </div>
        <div className="doc-card__stage">
          <span className="stage__label stage__label--active">{doc.stage_label}</span>
          <ProgressBar value={doc.progress} label={doc.stage_label} />
        </div>
        <div className="doc-card__actions">
          <span className="status status--processing">
            <Spinner /> Обрабатывается
          </span>
        </div>
      </li>
    );
  }
  const reviewDone = doc.review.total > 0 && doc.review.checked === doc.review.total;
  return (
    <li className="doc-card">
      <FileTypeIcon filename={doc.filename} kind={doc.kind} />
      <div className="doc-card__name">
        <Link to={`/documents/${doc.id}`} className="doc-card__title doc-card__link">
          {doc.filename}
        </Link>
        <div className="doc-card__meta">
          {documentMeta(doc)}
          {lowQuality && <span className="text-warning"> · {lowQuality}</span>}
        </div>
      </div>
      <FindingsBadges findings={doc.findings} worst={doc.worst_level} small />
      <div className="doc-card__review">
        {doc.review.total ? (
          <>
            <span className={reviewDone ? 'text-ok' : 'muted'}>
              Проверено {doc.review.checked} из {doc.review.total}
            </span>
            <ProgressBar value={(doc.review.checked / doc.review.total) * 100} tone={reviewDone ? 'ok' : undefined} label="Проверено находок" />
          </>
        ) : (
          <span className="muted">Замечаний нет</span>
        )}
      </div>
      <div className="doc-card__actions">
        <Link to={`/documents/${doc.id}`} className={primary ? 'btn btn--primary' : 'btn btn--secondary'}>
          Открыть
        </Link>
        <Button icon onClick={onDownload} loading={downloading} aria-label={`Скачать документ ${doc.filename}`} title="Скачать документ">
          {!downloading && <Download size={18} />}
        </Button>
      </div>
    </li>
  );
}

function FindingsTab({ batch, ruleId, onRuleChange }: { batch: BatchView; ruleId: string; onRuleChange: (id: string) => void }) {
  const navigate = useNavigate();
  const [level, setLevel] = useState<Level | ''>('');
  const [status, setStatus] = useState<FindingStatus | ''>('');
  const [page, setPage] = useState(1);
  const params = { level: level || undefined, status: status || undefined, rule_id: ruleId || undefined, page, page_size: FINDINGS_PAGE };
  const query = useQuery({
    queryKey: [...keys.batchFindings(batch.id), params],
    queryFn: () => api.batchFindings(batch.id, params),
    placeholderData: keepPreviousData,
  });
  const pages = query.data ? Math.max(1, Math.ceil(query.data.total / FINDINGS_PAGE)) : 1;
  return (
    <div className="card">
      <div className="card__header findings-filters">
        <select className="select select--sm" aria-label="Уровень" value={level} onChange={(e) => { setLevel(e.target.value as Level | ''); setPage(1); }}>
          <option value="">Все уровни</option>
          <option value="critical">Критично</option>
          <option value="warning">Внимание</option>
        </select>
        <select className="select select--sm" aria-label="Правило" value={ruleId} onChange={(e) => { onRuleChange(e.target.value); setPage(1); }}>
          <option value="">Все правила</option>
          {batch.rules_triggered?.map((r) => (
            <option key={r.rule_id} value={r.rule_id}>
              {r.code} {r.title}
            </option>
          ))}
        </select>
        <select className="select select--sm" aria-label="Статус" value={status} onChange={(e) => { setStatus(e.target.value as FindingStatus | ''); setPage(1); }}>
          <option value="">Все статусы</option>
          {(Object.keys(FINDING_STATUS_LABELS) as FindingStatus[]).map((s) => (
            <option key={s} value={s}>
              {FINDING_STATUS_LABELS[s]}
            </option>
          ))}
        </select>
        <div className="spacer" />
        {query.data && <span className="muted">Найдено: {query.data.total}</span>}
      </div>
      {query.isPending ? (
        <ListSkeleton rows={5} height={40} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : !query.data.items.length ? (
        <EmptyState icon={<SearchX size={28} />} title="Находок нет" text="Под выбранные фильтры не подходит ни одна находка." />
      ) : (
        <table className="table table--clickable findings-table">
          <thead>
            <tr>
              <th>Уровень</th>
              <th>Правило</th>
              <th>Документ</th>
              <th>Раздел</th>
              <th>Цитата</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {query.data.items.map((f) => (
              <tr key={f.id} tabIndex={0} onClick={() => navigate(findingLink(f.document_id, f.id))} onKeyDown={(e) => e.key === 'Enter' && navigate(findingLink(f.document_id, f.id))}>
                <td>
                  <LevelBadge level={f.level} small />
                </td>
                <td>
                  {f.rule ? (
                    <>
                      <span className="mono subtle">{f.rule.code}</span> {f.rule.title}
                    </>
                  ) : (
                    <span>Ручная пометка · {f.author?.full_name}</span>
                  )}
                </td>
                <td className="nowrap">{f.document?.filename}</td>
                <td className="nowrap">{[f.section_number && `п. ${f.section_number}`, f.page && `стр. ${f.page}`].filter(Boolean).join(', ') || '—'}</td>
                <td className="findings-table__quote">«{f.quote}»</td>
                <td className="nowrap">
                  <span className={f.status === 'rejected' ? 'subtle' : f.status === 'new' ? undefined : 'text-ok'}>{FINDING_STATUS_LABELS[f.status]}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {pages > 1 && (
        <div className="card__body pagination" style={{ justifyContent: 'flex-end' }}>
          <Button size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            Назад
          </Button>
          <span className="muted">
            {page} из {pages}
          </span>
          <Button size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
            Вперёд
          </Button>
        </div>
      )}
    </div>
  );
}

function SubjectDialog({ batch, onClose, onSaved }: { batch: BatchView; onClose: () => void; onSaved: () => void }) {
  const qc = useQueryClient();
  const [subject, setSubject] = useState(batch.subject ?? '');
  const mutation = useMutation({
    mutationFn: () => api.updateBatch(batch.id, { subject: subject.trim() }),
    onSuccess: (view) => {
      qc.setQueryData(keys.batch(batch.id), view);
      onSaved();
    },
  });
  return (
    <Dialog
      title="Предмет закупки"
      onClose={onClose}
      actions={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" disabled={!subject.trim()} loading={mutation.isPending} onClick={() => mutation.mutate()}>
            Сохранить
          </Button>
        </>
      }
    >
      <p style={{ marginBottom: 14 }}>Система не нашла предмет закупки в извещении. Он нужен правилам, которые сравнивают условия с предметом — например, срок поставки.</p>
      <Field label="Предмет закупки" htmlFor="subject" error={mutation.error ? errorMessage(mutation.error) : null}>
        <input id="subject" className="input" autoFocus value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Поставка серверного оборудования" />
      </Field>
    </Dialog>
  );
}

function PassportCard({ fields }: { fields: PassportField[] }) {
  return (
    <section className="card side-card">
      <h3>Паспорт закупки</h3>
      <p className="muted side-card__hint">Ключевые условия из документов пакета. Нажмите, чтобы открыть место в документе</p>
      <dl className="passport">
        {fields.map((f) => (
          <Link key={f.key} className="passport__row" to={`/documents/${f.document_id}?block=${f.block_id}&start=${f.start}&end=${f.end}`} title={`«${f.quote}» — ${f.filename ?? ''}`}>
            <dt>{f.label}</dt>
            <dd>{f.value}</dd>
          </Link>
        ))}
      </dl>
    </section>
  );
}

export function BatchPage() {
  const { batchId = '' } = useParams();
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const query = useBatch(batchId);
  const batch = query.data;
  const running = isBatchRunning(batch);
  useBatchEvents(batchId, running);
  const exporter = useExport();
  const { retry, isPending: retrying } = useRetryDocument(batchId);
  const [filter, setFilter] = useState<DocFilter>('all');
  const tab: Tab = sp.get('tab') === 'findings' ? 'findings' : 'documents';
  const ruleId = sp.get('rule') ?? '';
  const [dialog, setDialog] = useState<'rename' | 'delete' | 'rerun' | 'subject' | null>(null);

  const setTab = (next: Tab, rule?: string) => {
    const out = new URLSearchParams(sp);
    if (next === 'findings') out.set('tab', 'findings');
    else out.delete('tab');
    if (rule !== undefined) {
      if (rule) out.set('rule', rule);
      else out.delete('rule');
    }
    setSp(out, { replace: true });
  };

  const rerun = useMutation({
    mutationFn: () => api.rerunBatch(batchId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: keys.batch(batchId) });
      void qc.invalidateQueries({ queryKey: keys.batches() });
      setDialog(null);
      navigate(`/batches/${batchId}/processing`);
    },
    onError: (e) => {
      setDialog(null);
      toast.error(e);
    },
  });

  if (query.isError) {
    return (
      <div className="page">
        <Breadcrumbs items={[{ label: 'Пакеты', to: '/batches' }, { label: 'Пакет' }]} />
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  if (!batch) {
    return (
      <div className="page stack" style={{ gap: 20 }}>
        <Skeleton width={180} />
        <Skeleton width={320} height={36} />
        <Skeleton width={560} />
        <div className="counter-cards">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} height={96} />
          ))}
        </div>
        <ListSkeleton rows={4} height={72} />
      </div>
    );
  }

  const docs = batch.documents.filter((d) => matchesFilter(d, filter));
  const review = batch.review ?? { checked: 0, total: 0 };
  const findingsTotal = (batch.findings?.critical ?? 0) + (batch.findings?.warning ?? 0);
  const passport = batch.meta?.passport?.fields ?? [];
  const firstDone = batch.documents.find((d) => d.status === 'done');
  const progress = batch.progress;

  return (
    <div className="page page--wide">
      <header className="page__header">
        <div className="page__heading">
          <Breadcrumbs items={[{ label: 'Пакеты', to: '/batches' }, { label: batch.name }]} />
          <h1>{batch.name}</h1>
          <p className="page__subtitle">
            {batch.subject ? (
              batch.subject
            ) : running ? (
              <span className="subtle">Предмет закупки определяется…</span>
            ) : (
              <>
                <span className="text-warning">Предмет закупки не определён</span>{' '}
                <button type="button" className="btn btn--link" onClick={() => setDialog('subject')}>
                  Указать
                </button>
              </>
            )}
            {' · '}загружен {formatDateTime(batch.created_at)}
            {batch.author && ` · ${batch.author.full_name}`}
            {batch.profile && ` · ${batch.profile.name}`}
          </p>
          <div className="batch-actions">
            {review.total > 0 && (
              <div className="batch-review">
                <span>
                  Проверено {review.checked} из {review.total} находок
                </span>
                <ProgressBar value={(review.checked / review.total) * 100} tone={review.checked === review.total ? 'ok' : undefined} label="Проверено находок" />
              </div>
            )}
            <Button variant="primary" loading={exporter.isPending(`batch:${batch.id}`)} disabled={running} onClick={() => void exporter.downloadBatch(batch.id)}>
              {!exporter.isPending(`batch:${batch.id}`) && <Download size={18} />}
              Скачать пакет
            </Button>
            <Menu
              trigger={(props) => (
                <Button icon aria-label="Другие действия с пакетом" {...props}>
                  <MoreHorizontal size={18} />
                </Button>
              )}
              align="left"
              items={[
                { label: 'Переименовать', icon: <Pencil size={16} />, onClick: () => setDialog('rename') },
                { label: 'Перезапустить проверку', icon: <RotateCw size={16} />, disabled: running, onClick: () => setDialog('rerun') },
                'separator',
                { label: 'Удалить пакет', icon: <Trash2 size={16} />, danger: true, onClick: () => setDialog('delete') },
              ]}
            />
          </div>
        </div>
      </header>

      {running && progress && (
        <div className="banner banner--info" style={{ marginBottom: 20 }} role="status">
          <Loader size={18} className="spin" />
          <span>
            Пакет ещё обрабатывается: готово {progress.done} из {progress.total}. Готовые документы уже можно открывать.
          </span>
          <div className="spacer" />
          <Link to={`/batches/${batch.id}/processing`} className="btn btn--secondary btn--sm">
            Ход обработки
          </Link>
        </div>
      )}

      <CounterCards
        batch={batch}
        filter={filter}
        onChange={(f) => {
          setFilter(f);
          if (tab !== 'documents') setTab('documents');
        }}
      />

      <div className="batch-layout">
        <div className="batch-layout__main">
          <div className="tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === 'documents'} className={tab === 'documents' ? 'tab tab--active' : 'tab'} onClick={() => setTab('documents')}>
              Документы <span className="tab__count">· {batch.documents.length}</span>
            </button>
            <button type="button" role="tab" aria-selected={tab === 'findings'} className={tab === 'findings' ? 'tab tab--active' : 'tab'} onClick={() => setTab('findings')}>
              Все находки <span className="tab__count">· {findingsTotal}</span>
            </button>
          </div>
          <div className="section-gap" style={{ marginTop: 16 }}>
            {tab === 'documents' ? (
              docs.length ? (
                <ul className="doc-cards">
                  {docs.map((doc) => (
                    <DocumentRow
                      key={doc.id}
                      doc={doc}
                      primary={doc.id === firstDone?.id}
                      onRetry={() => void retry(doc.id)}
                      retrying={retrying(doc.id)}
                      onDownload={() => void exporter.downloadDocument(doc.id)}
                      downloading={exporter.isPending(`document:${doc.id}`)}
                    />
                  ))}
                </ul>
              ) : (
                <div className="card">
                  <EmptyState
                    icon={<SearchX size={28} />}
                    title="Таких документов нет"
                    actions={<Button onClick={() => setFilter('all')}>Показать все документы</Button>}
                  />
                </div>
              )
            ) : (
              <FindingsTab batch={batch} ruleId={ruleId} onRuleChange={(id) => setTab('findings', id)} />
            )}
          </div>
        </div>

        <aside className="batch-layout__side">
          <section className="card side-card">
            <h3>Сработавшие правила</h3>
            {batch.rules_triggered?.length ? (
              <>
                <p className="muted side-card__hint">Нажмите на правило, чтобы отфильтровать находки</p>
                <ul className="rules-triggered">
                  {batch.rules_triggered.map((r) => (
                    <li key={r.rule_id}>
                      <button
                        type="button"
                        className={ruleId === r.rule_id && tab === 'findings' ? 'rules-triggered__item rules-triggered__item--active' : 'rules-triggered__item'}
                        onClick={() => setTab('findings', ruleId === r.rule_id && tab === 'findings' ? '' : r.rule_id)}
                      >
                        <LevelDot level={r.level} />
                        <span className="mono subtle">{r.code}</span>
                        <span className="rules-triggered__title">{r.title}</span>
                        <span className="tabular">{r.count}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="muted side-card__hint">{running ? 'Появятся, когда документы будут проверены.' : 'Ни одно правило не сработало.'}</p>
            )}
          </section>
          {passport.length > 0 && <PassportCard fields={passport} />}
          {batch.counters && batch.counters.errors > 0 && (
            <div className="note">
              <AlertCircle size={16} />
              <span>Документы с ошибкой не проверены и не влияют на цвет пакета. Их можно повторить из списка.</span>
            </div>
          )}
        </aside>
      </div>

      {dialog === 'rename' && <RenameBatchDialog batch={batch} onClose={() => setDialog(null)} />}
      {dialog === 'delete' && <DeleteBatchDialog batch={batch} onClose={() => setDialog(null)} onDeleted={() => navigate('/batches')} />}
      {dialog === 'rerun' && (
        <ConfirmDialog
          title="Перезапустить проверку?"
          text="Все документы будут проверены заново актуальными версиями правил. Находки ИИ появятся заново, ручные пометки сохранятся."
          confirmLabel="Перезапустить"
          loading={rerun.isPending}
          onConfirm={() => rerun.mutate()}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog === 'subject' && (
        <SubjectDialog
          batch={batch}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            toast.show('Предмет закупки сохранён. Чтобы правила его учли, перезапустите проверку', {
              action: { label: 'Перезапустить', onClick: () => setDialog('rerun') },
            });
          }}
        />
      )}
    </div>
  );
}
