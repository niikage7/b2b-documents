import { getMockTransport, MOCK_API } from './http';
import { BATCH_EVENT_TYPES, type BatchEvent } from './types';

/**
 * Поток событий пакета `GET /api/batches/{id}/events` (SSE).
 *
 * EventSource сам переподключается и досылает `Last-Event-ID`; если пропущенных событий на
 * сервере уже нет, первым придёт `batch.snapshot`. Возвращает функцию отписки.
 */
export function subscribeBatchEvents(
  batchId: string,
  onEvent: (event: BatchEvent) => void,
  onConnectionChange?: (connected: boolean) => void,
): () => void {
  if (MOCK_API) {
    let unsubscribe: (() => void) | null = null;
    let closed = false;
    void getMockTransport().then((t) => {
      if (closed) return;
      unsubscribe = t.subscribe(batchId, onEvent);
      onConnectionChange?.(true);
    });
    return () => {
      closed = true;
      unsubscribe?.();
    };
  }

  const source = new EventSource(`/api/batches/${batchId}/events`, { withCredentials: true });
  source.onopen = () => onConnectionChange?.(true);
  source.onerror = () => onConnectionChange?.(false);
  for (const type of BATCH_EVENT_TYPES) {
    source.addEventListener(type, (e) => {
      try {
        onEvent({ type, data: JSON.parse((e as MessageEvent<string>).data) } as BatchEvent);
      } catch {
        // Битое событие пропускаем: следующее или snapshot вернут актуальное состояние
      }
    });
  }
  return () => source.close();
}
