import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/endpoints';
import { ApiError } from '../api/http';
import { useToast } from '../components/feedback';

/**
 * Выгрузка: POST ставит задачу в очередь export, затем раз в секунду GET /api/exports/{id},
 * пока файл не готов. Готовый файл отдаёт сам API: ссылка работает с cookie входа.
 */
export function useExport() {
  const toast = useToast();
  const [pending, setPending] = useState<Set<string>>(new Set());
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const run = useCallback(
    async (key: string, start: () => Promise<{ export_id: string }>) => {
      setPending((s) => new Set(s).add(key));
      try {
        const { export_id } = await start();
        for (let i = 0; i < 600 && alive.current; i++) {
          await new Promise((r) => setTimeout(r, 1000));
          const status = await api.getExport(export_id);
          if (status.status === 'error') throw new ApiError(500, 'export_failed', status.error_message || 'Не удалось подготовить файл');
          if (status.status === 'done' && status.download_url) {
            const a = document.createElement('a');
            a.href = status.download_url;
            if (status.filename) a.download = status.filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            toast.show(`Файл готов: ${status.filename ?? 'скачивание началось'}`);
            return;
          }
        }
      } catch (e) {
        toast.error(e);
      } finally {
        setPending((s) => {
          const next = new Set(s);
          next.delete(key);
          return next;
        });
      }
    },
    [toast],
  );

  return {
    isPending: (key: string) => pending.has(key),
    downloadDocument: (documentId: string) => run(`document:${documentId}`, () => api.exportDocument(documentId)),
    downloadBatch: (batchId: string) => run(`batch:${batchId}`, () => api.exportBatch(batchId)),
    downloadClarification: (batchId: string) => run(`clarification:${batchId}`, () => api.clarification(batchId)),
  };
}
