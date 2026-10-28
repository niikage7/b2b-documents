import { keepPreviousData, type QueryClient, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from './endpoints';
import { subscribeBatchEvents } from './events';
import type { BatchEvent, BatchListParams, BatchView, DocumentCard, DocumentView, FindingMutation, Recalc } from './types';

export const keys = {
  me: ['me'] as const,
  config: ['config'] as const,
  profiles: ['profiles'] as const,
  batches: (params?: BatchListParams) => (params ? (['batches', params] as const) : (['batches'] as const)),
  batch: (id: string) => ['batch', id] as const,
  batchFindings: (id: string) => ['batch', id, 'findings'] as const,
  document: (id: string) => ['document', id] as const,
  history: (findingId: string) => ['history', findingId] as const,
  rules: (profileId: string) => ['rules', profileId] as const,
  rule: (id: string) => ['rule', id] as const,
  systemVariables: ['system-variables'] as const,
  recentDocuments: ['recent-documents'] as const,
};

export function useConfig() {
  return useQuery({ queryKey: keys.config, queryFn: api.config, staleTime: Infinity });
}

export function useProfiles() {
  return useQuery({ queryKey: keys.profiles, queryFn: api.listProfiles, staleTime: 60_000 });
}

export function useBatches(params: BatchListParams) {
  return useQuery({
    queryKey: keys.batches(params),
    queryFn: () => api.listBatches(params),
    placeholderData: keepPreviousData,
    // Список с пакетами в работе обновляем сам: статус и счётчики меняются без перезагрузки
    refetchInterval: (q) => (q.state.data?.processing_count ? 4000 : false),
  });
}

export function useBatch(id: string) {
  return useQuery({ queryKey: keys.batch(id), queryFn: () => api.getBatch(id) });
}

export function useDocument(id: string) {
  return useQuery({ queryKey: keys.document(id), queryFn: () => api.getDocument(id) });
}

function patchDocument(view: BatchView, documentId: string, patch: Partial<DocumentCard>): BatchView {
  return { ...view, documents: view.documents.map((d) => (d.id === documentId ? { ...d, ...patch } : d)) };
}

/** Событие SSE → кэш пакета. Итоговые события перечитывают пакет целиком: в них не все поля. */
export function applyBatchEvent(qc: QueryClient, batchId: string, event: BatchEvent) {
  const key = keys.batch(batchId);
  switch (event.type) {
    case 'batch.snapshot':
      qc.setQueryData(key, event.data);
      break;
    case 'batch.progress':
      qc.setQueryData<BatchView>(key, (v) => v && { ...v, status: event.data.status, progress: event.data.progress, eta_seconds: event.data.eta_seconds });
      break;
    case 'batch.meta':
      qc.setQueryData<BatchView>(key, (v) => v && { ...v, subject: event.data.subject });
      break;
    case 'document.progress': {
      const { document_id, ...patch } = event.data;
      qc.setQueryData<BatchView>(key, (v) => v && patchDocument(v, document_id, patch));
      qc.setQueryData<DocumentView>(keys.document(document_id), (d) => d && { ...d, status: patch.status, stage: patch.stage, stage_label: patch.stage_label, progress: patch.progress });
      break;
    }
    case 'document.done':
    case 'document.error': {
      const { document_id } = event.data;
      if (event.type === 'document.done') {
        const { worst_level, findings, review, pages, ocr_low_quality_pages } = event.data;
        qc.setQueryData<BatchView>(key, (v) =>
          v && patchDocument(v, document_id, { status: 'done', stage: 'done', stage_label: 'Готово', progress: 100, next_stage_label: null, worst_level, findings, review, pages, ocr_low_quality_pages }),
        );
      } else {
        qc.setQueryData<BatchView>(key, (v) =>
          v && patchDocument(v, document_id, { status: 'error', stage: 'error', stage_label: 'Ошибка', worst_level: 'not_checked', error: event.data.error }),
        );
      }
      void qc.invalidateQueries({ queryKey: key, exact: true });
      void qc.invalidateQueries({ queryKey: keys.document(document_id) });
      break;
    }
    case 'batch.done':
      void qc.invalidateQueries({ queryKey: key });
      void qc.invalidateQueries({ queryKey: keys.batches() });
      break;
    case 'export.done':
      break;
  }
}

/** Живой пакет: подписка на SSE, пока пакет или документ обрабатывается. */
export function useBatchEvents(batchId: string | undefined, active: boolean) {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(true);
  useEffect(() => {
    if (!batchId || !active) return;
    return subscribeBatchEvents(batchId, (e) => applyBatchEvent(qc, batchId, e), setConnected);
  }, [batchId, active, qc]);
  return connected;
}

/** После правки находки: счётчики документа и пакета из ответа API — без лишних запросов. */
export function applyRecalc(qc: QueryClient, batchId: string, documentId: string, recalc: Recalc) {
  qc.setQueryData<DocumentView>(keys.document(documentId), (d) =>
    d && { ...d, worst_level: recalc.document.worst_level, findings_summary: recalc.document.findings_summary, review: recalc.document.review },
  );
  qc.setQueryData<BatchView>(keys.batch(batchId), (v) =>
    v && {
      ...patchDocument(v, documentId, { worst_level: recalc.document.worst_level, findings: recalc.document.findings_summary, review: recalc.document.review }),
      worst_level: recalc.batch.worst_level,
      counters: recalc.batch.counters,
      review: recalc.batch.review,
      findings: recalc.batch.findings ?? v.findings,
    },
  );
  // rules_triggered и список пакетов пересчитает сервер
  void qc.invalidateQueries({ queryKey: keys.batch(batchId), exact: true, refetchType: 'none' });
  void qc.invalidateQueries({ queryKey: keys.batchFindings(batchId) });
  void qc.invalidateQueries({ queryKey: keys.batches(), refetchType: 'none' });
}

export function applyFindingMutation(qc: QueryClient, batchId: string, result: FindingMutation) {
  const documentId = result.finding.document_id;
  qc.setQueryData<DocumentView>(keys.document(documentId), (d) => {
    if (!d) return d;
    const exists = d.findings.some((f) => f.id === result.finding.id);
    const findings = exists ? d.findings.map((f) => (f.id === result.finding.id ? result.finding : f)) : [...d.findings, result.finding];
    return { ...d, findings };
  });
  applyRecalc(qc, batchId, documentId, result);
  void qc.invalidateQueries({ queryKey: keys.history(result.finding.id) });
}
