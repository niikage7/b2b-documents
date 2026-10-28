import type { ApiErrorBody, BatchEvent } from './types';

/** Встроенный мок API вместо бэкенда: `npm run dev:mock` или VITE_MOCK_API=true. */
export const MOCK_API = import.meta.env.VITE_MOCK_API === 'true';

export interface TransportRequest {
  method: string;
  path: string;
  query: URLSearchParams;
  body?: unknown;
  files?: File[];
}

export interface TransportResponse {
  status: number;
  body?: unknown;
}

/** То, что реализует мок в src/mocks: тот же контракт, что у HTTP и SSE настоящего API. */
export interface MockTransport {
  handle(req: TransportRequest, onUploadProgress?: (loaded: number, total: number) => void): Promise<TransportResponse>;
  subscribe(batchId: string, listener: (event: BatchEvent) => void): () => void;
}

let mockTransport: Promise<MockTransport> | null = null;

export function getMockTransport(): Promise<MockTransport> {
  mockTransport ??= import('../mocks/server').then((m) => m.createMockTransport());
  return mockTransport;
}

/** Ошибка API: `message` написан по-русски и показывается пользователю как есть. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** Ошибки полей из 422: `{"variables[0].name": "…"}`. */
  get fields(): Record<string, string> {
    const fields = this.details.fields;
    return fields && typeof fields === 'object' ? (fields as Record<string, string>) : {};
  }
}

const FALLBACK_MESSAGES: Record<number, string> = {
  0: 'Нет связи с сервером. Проверьте подключение и повторите',
  401: 'Сессия завершена, войдите снова',
  403: 'Недостаточно прав',
  404: 'Не найдено',
  409: 'Действие недоступно в текущем состоянии',
  413: 'Пакет больше 100 МБ',
  422: 'Проверьте заполнение полей',
  429: 'Слишком много попыток. Попробуйте позже',
  500: 'Ошибка на сервере. Повторите через минуту',
  502: 'Сервер недоступен. Повторите через минуту',
  503: 'Сервер недоступен. Повторите через минуту',
  504: 'Сервер не успел ответить. Повторите через минуту',
};

function toApiError(status: number, body: unknown): ApiError {
  const err = (body as ApiErrorBody | undefined)?.error;
  if (err && typeof err.message === 'string') {
    return new ApiError(status, err.code, err.message, err.details ?? {});
  }
  const fallback = FALLBACK_MESSAGES[status] ?? (status >= 500 ? FALLBACK_MESSAGES[500] : 'Не удалось выполнить запрос');
  return new ApiError(status, `http_${status}`, fallback);
}

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

/** Вызывается на любой 401, кроме самого входа: приложение отправляет на экран входа. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null) {
  onUnauthorized = handler;
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** Не отправлять на вход при 401 (сам вход и первичная проверка сессии). */
  skipAuthRedirect?: boolean;
}

function toSearchParams(query?: Query): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value));
  }
  return params;
}

function handleStatus(status: number, body: unknown, opts: RequestOptions) {
  if (status >= 200 && status < 300) return;
  const error = toApiError(status, body);
  if (status === 401 && !opts.skipAuthRedirect) onUnauthorized?.();
  throw error;
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const query = toSearchParams(opts.query);
  if (MOCK_API) {
    const transport = await getMockTransport();
    const res = await transport.handle({ method, path, query, body: opts.body });
    handleStatus(res.status, res.body, opts);
    return res.body as T;
  }
  const qs = query.toString();
  let res: Response;
  try {
    res = await fetch(`/api${path}${qs ? `?${qs}` : ''}`, {
      method,
      credentials: 'include',
      headers: opts.body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    throw new ApiError(0, 'network', FALLBACK_MESSAGES[0]);
  }
  const text = res.status === 204 ? '' : await res.text();
  let body: unknown = undefined;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = undefined;
    }
  }
  handleStatus(res.status, body, opts);
  return body as T;
}

/** multipart-загрузка с прогрессом: fetch не умеет сообщать о прогрессе отправки, поэтому XHR. */
export async function upload<T>(
  path: string,
  files: File[],
  onProgress?: (loaded: number, total: number) => void,
): Promise<T> {
  if (MOCK_API) {
    const transport = await getMockTransport();
    const res = await transport.handle({ method: 'POST', path, query: new URLSearchParams(), files }, onProgress);
    handleStatus(res.status, res.body, {});
    return res.body as T;
  }
  return new Promise<T>((resolve, reject) => {
    const form = new FormData();
    for (const file of files) form.append('files', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api${path}`);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded, e.total);
    };
    xhr.onerror = () => reject(new ApiError(0, 'network', FALLBACK_MESSAGES[0]));
    xhr.onload = () => {
      let body: unknown = undefined;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
      } catch {
        body = undefined;
      }
      try {
        handleStatus(xhr.status, body, {});
        resolve(body as T);
      } catch (e) {
        reject(e);
      }
    };
    xhr.send(form);
  });
}
