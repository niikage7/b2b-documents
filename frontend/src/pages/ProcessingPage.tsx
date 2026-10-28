import { useQueryClient } from '@tanstack/react-query';
import { AlertCircle, ArrowRight, CheckCircle2, Info, RotateCw, WifiOff } from 'lucide-react';
import { useState } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router';
import { api } from '../api/endpoints';
import { keys, useBatch, useBatchEvents } from '../api/queries';
import type { BatchView, DocumentCard } from '../api/types';
import { ErrorState, ListSkeleton, useToast } from '../components/feedback';
import { Breadcrumbs, Button, FileTypeIcon, FindingsBadges, ProgressBar, Skeleton, Spinner } from '../components/ui';
import { countLabel, formatBytes, formatEta } from '../lib/format';
import { documentTypeLabel } from '../lib/labels';

export function isBatchRunning(batch: Pick<BatchView, 'status' | 'documents'> | undefined) {
  if (!batch) return false;
  return batch.status === 'queued' || batch.status === 'processing' || batch.documents.some((d) => d.status === 'queued' || d.status === 'processing');
}

export function documentMeta(doc: Pick<DocumentCard, 'kind' | 'pages' | 'size_bytes'>) {
  return [documentTypeLabel(doc.kind), doc.pages ? `${doc.pages} стр.` : formatBytes(doc.size_bytes)].join(' · ');
}

export function lowQualityNote(pages: number[]) {
  if (!pages.length) return null;
  return `${countLabel(pages.length, ['страница распознана', 'страницы распознаны', 'страниц распознано'])} с низким качеством`;
}

/** Повтор упавшего документа: он продолжит с этапа, на котором упал. */
export function useRetryDocument(batchId: string) {
  const qc = useQueryClient();
  const toast = useToast();
  const [pending, setPending] = useState<Set<string>>(new Set());
  const retry = async (documentId: string) => {
    setPending((s) => new Set(s).add(documentId));
    try {
      await api.retryDocument(documentId);
      qc.setQueryData<BatchView>(keys.batch(batchId), (v) =>
        v && {
          ...v,
          status: 'processing',
          documents: v.documents.map((d) =>
            d.id === documentId ? { ...d, status: 'queued', stage: 'queued', stage_label: 'В очереди', progress: 0, error: null, worst_level: null } : d,
          ),
        },
      );
      toast.show('Документ отправлен на повторную обработку');
    } catch (e) {
      toast.error(e);
    } finally {
      setPending((s) => {
        const next = new Set(s);
        next.delete(documentId);
        return next;
      });
    }
  };
  return { retry, isPending: (id: string) => pending.has(id) };
}

function StageCell({ doc }: { doc: DocumentCard }) {
  if (doc.status === 'error') {
    return (
      <div className="stage stage--error">
        <AlertCircle size={16} />
        <span>
          Ошибка: {doc.error?.message?.replace(/^./, (c) => c.toLowerCase()) ?? 'не удалось обработать файл'}.
          <br />
          Остальные документы обрабатываются.
        </span>
      </div>
    );
  }
  const done = doc.status === 'done';
  return (
    <div className="stage">
      <span className={done ? 'text-ok stage__label' : doc.status === 'queued' ? 'muted stage__label' : 'stage__label stage__label--active'}>
        {doc.stage_label}
      </span>
      <ProgressBar value={doc.progress} tone={done ? 'ok' : undefined} label={`${doc.filename}: ${doc.stage_label}`} />
    </div>
  );
}

export function ProcessingPage() {
  const { batchId = '' } = useParams();
  const navigate = useNavigate();
  const query = useBatch(batchId);
  const batch = query.data;
  const running = isBatchRunning(batch);
  const connected = useBatchEvents(batchId, running);
  const { retry, isPending } = useRetryDocument(batchId);

  if (batch?.status === 'draft') return <Navigate to={`/upload?draft=${batch.id}`} replace />;

  if (query.isError) {
    return (
      <div className="page">
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const progress = batch?.progress;
  const readyCount = batch?.documents.filter((d) => d.status === 'done').length ?? 0;
  const eta = formatEta(batch?.eta_seconds);
  const counters = batch?.counters;

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <Breadcrumbs items={[{ label: 'Пакеты', to: '/batches' }, { label: batch?.name ?? '…' }]} />
          <div className="row" style={{ gap: 12 }}>
            {batch ? <h1>{batch.name}</h1> : <Skeleton width={260} height={36} />}
            {batch?.profile && <span className="badge">{batch.profile.name}</span>}
          </div>
          {progress && (
            <p className="page__subtitle">
              {running ? `Обработано ${progress.done} из ${progress.total}` : `Обработано ${countLabel(progress.total, ['документ', 'документа', 'документов'])}`}
              {progress.errors > 0 && ` · ${countLabel(progress.errors, ['ошибка', 'ошибки', 'ошибок'])}`}
              {running && eta && ` · ${eta}`}
            </p>
          )}
        </div>
        <div className="page__actions">
          <Button variant="primary" size="lg" disabled={!readyCount} onClick={() => navigate(`/batches/${batchId}`)}>
            Перейти к разбору <ArrowRight size={18} />
          </Button>
        </div>
      </header>

      {progress && <ProgressBar value={running ? progress.percent : 100} tone={running ? undefined : 'ok'} large label="Общий прогресс" />}

      {!running && batch && counters && (
        <div className={counters.errors ? 'banner banner--warning section-gap' : 'banner banner--ok section-gap'} role="status">
          <CheckCircle2 size={20} />
          <span>
            Проверка завершена
            {counters.critical > 0 && ` · Критично ${counters.critical}`}
            {counters.warning > 0 && ` · Внимание ${counters.warning}`}
            {counters.ok > 0 && ` · Без рисков ${counters.ok}`}
            {counters.errors > 0 && ` · С ошибкой ${counters.errors}`}
          </span>
          <div className="spacer" />
          <Link to={`/batches/${batchId}`} className="btn btn--primary btn--sm">
            Открыть разбор пакета
          </Link>
        </div>
      )}

      {running && !connected && (
        <div className="banner banner--warning section-gap" role="status">
          <WifiOff size={18} /> Связь с сервером прервалась — переподключаемся. Обработка на сервере продолжается.
        </div>
      )}

      <section className="card section-gap">
        <div className="card__header">
          <h2>Документы пакета</h2>
        </div>
        {!batch ? (
          <ListSkeleton rows={4} />
        ) : batch.documents.length === 0 ? (
          <div className="card__body muted row">
            <Spinner /> Распаковываем пакет…
          </div>
        ) : (
          <ul className="doc-progress">
            {batch.documents.map((doc) => {
              const lowQuality = lowQualityNote(doc.ocr_low_quality_pages);
              return (
                <li key={doc.id} className={`doc-progress__row doc-progress__row--${doc.status}`}>
                  <FileTypeIcon filename={doc.filename} kind={doc.kind} small />
                  <div className="doc-progress__name">
                    <div className="file-row__name">{doc.filename}</div>
                    <div className="file-row__meta">
                      {documentMeta(doc)}
                      {lowQuality && <span className="text-warning"> · {lowQuality}</span>}
                    </div>
                  </div>
                  <StageCell doc={doc} />
                  <div className="doc-progress__result">
                    {doc.status === 'done' ? (
                      <FindingsBadges findings={doc.findings} worst={doc.worst_level} small />
                    ) : doc.status === 'error' ? (
                      <span className="subtle">—</span>
                    ) : (
                      <span className="muted">{doc.next_stage_label ?? ''}</span>
                    )}
                  </div>
                  <div className="doc-progress__action">
                    {doc.status === 'done' && (
                      <Link className="btn btn--secondary" to={`/documents/${doc.id}`}>
                        Открыть
                      </Link>
                    )}
                    {doc.status === 'error' && (
                      <Button onClick={() => void retry(doc.id)} loading={isPending(doc.id)}>
                        <RotateCw size={16} /> Повторить
                      </Button>
                    )}
                    {doc.status === 'processing' && <span className="status status--processing">Обрабатывается</span>}
                    {doc.status === 'queued' && <span className="status status--queued">В очереди</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <div className="note note--card section-gap" style={{ maxWidth: 900 }}>
        <Info size={16} />
        <span>
          Можно уйти с этой страницы — обработка продолжится, а пакет останется в списке «Пакеты» со статусом «Обрабатывается». Готовые
          документы уже можно открывать.
        </span>
      </div>
    </div>
  );
}
