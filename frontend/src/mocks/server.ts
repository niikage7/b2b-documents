// Мок API «Требований к бэкенду»: те же пути, коды ответов, ошибки и события SSE.
// Нужен, пока нет настоящего API (зона Влада): фронтенд работает с ним через тот же клиент.

import type { MockTransport, TransportRequest, TransportResponse } from '../api/http';
import type {
  ArchiveEntry,
  BatchEvent,
  FileCheck,
  FindingStatus,
  HistoryAction,
  Level,
  RuleInput,
  RuleTestFinding,
  UploadedFile,
} from '../api/types';
import { validateRule } from '../lib/ruleValidation';
import {
  buildBlocks,
  buildFindings,
  DEMO_PASSWORD,
  ERROR_MESSAGES,
  loadState,
  type MBatch,
  type MBlock,
  type MDocument,
  type MFinding,
  type MockState,
  type MRule,
  nowIso,
  passportFields,
  resetState,
  saveState,
  TEMPLATES,
  type TemplateName,
  templateNameFor,
  uid,
} from './state';
import {
  batchSummary,
  batchView,
  canTransition,
  documentCard,
  documentView,
  findingsPayload,
  hits30d,
  me,
  NEXT_STAGE_LABELS,
  recalc,
  ruleView,
  stageLabel,
  stripBlock,
} from './views';

const MAX_BATCH_BYTES = 100 * 1024 * 1024;
const ALLOWED = ['zip', 'pdf', 'docx', 'doc', 'rtf'];
const TICK_MS = 700;

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

const notFound = (message: string) => new HttpError(404, 'not_found', message);
const validation = (fields: Record<string, string>, code = 'validation_error', warnings?: string[]) =>
  new HttpError(422, code, Object.values(fields)[0] ?? 'Проверьте заполнение полей', warnings ? { fields, warnings } : { fields });

type Handler = (ctx: Ctx) => unknown | Promise<unknown>;

interface Ctx {
  params: Record<string, string>;
  query: URLSearchParams;
  body: Record<string, unknown>;
  files: File[];
  userId: string;
  onUploadProgress?: (loaded: number, total: number) => void;
}

interface Route {
  method: string;
  pattern: RegExp;
  keys: string[];
  handler: Handler;
  public?: boolean;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const latency = () => sleep(120 + Math.random() * 220);

export function createMockTransport(): MockTransport {
  let state: MockState = loadState();
  const listeners = new Map<string, Set<(e: BatchEvent) => void>>();
  const exportUrls = new Map<string, string>();
  const routes: Route[] = [];

  const persist = () => saveState(state);
  const emit = (batchId: string, event: BatchEvent) => {
    for (const listener of listeners.get(batchId) ?? []) listener(event);
  };

  function route(method: string, path: string, handler: Handler, opts: { public?: boolean } = {}) {
    const keys: string[] = [];
    const pattern = new RegExp(
      `^${path.replace(/:(\w+)/g, (_, key) => {
        keys.push(key);
        return '([^/]+)';
      })}$`,
    );
    routes.push({ method, pattern, keys, handler, public: opts.public });
  }

  // --- Доступ к данным ------------------------------------------------------------------------

  const user = (id: string) => state.users.find((u) => u.id === id)!;
  const requireMethodologist = (ctx: Ctx) => {
    if (user(ctx.userId).role !== 'methodologist') throw new HttpError(403, 'forbidden', 'Правила доступны только методологу');
  };
  const getBatch = (id: string): MBatch => {
    const batch = state.batches.find((b) => b.id === id && !b.deleted);
    if (!batch) throw notFound('Пакет не найден');
    return batch;
  };
  const getDoc = (id: string): MDocument => {
    const doc = state.documents.find((d) => d.id === id);
    if (!doc || !state.batches.some((b) => b.id === doc.batch_id && !b.deleted)) throw notFound('Документ не найден');
    return doc;
  };
  const getFinding = (id: string): MFinding => {
    const f = state.findings.find((x) => x.id === id);
    if (!f) throw notFound('Находка не найдена');
    return f;
  };
  const getRule = (id: string): MRule => {
    const r = state.rules.find((x) => x.id === id && !x.deleted);
    if (!r) throw notFound('Правило не найдено');
    return r;
  };
  const defaultProfile = () => state.profiles.find((p) => p.is_default)!;

  // --- Авторизация и настройки ----------------------------------------------------------------

  route(
    'POST',
    '/auth/login',
    (ctx) => {
      const login = String(ctx.body.login ?? '').trim().toLowerCase();
      const password = String(ctx.body.password ?? '');
      const u = state.users.find((x) => x.login.toLowerCase() === login || x.email.toLowerCase() === login);
      if (!u) throw new HttpError(401, 'invalid_credentials', 'Неверный логин или пароль');
      if (u.locked_until && u.locked_until > Date.now()) {
        throw new HttpError(429, 'too_many_attempts', 'Слишком много попыток входа. Попробуйте через несколько минут', {
          retry_after_seconds: Math.ceil((u.locked_until - Date.now()) / 1000),
        });
      }
      if (password !== DEMO_PASSWORD) {
        u.failed_logins += 1;
        if (u.failed_logins >= 5) {
          u.failed_logins = 0;
          u.locked_until = Date.now() + 5 * 60_000;
        }
        persist();
        throw new HttpError(401, 'invalid_credentials', 'Неверный логин или пароль');
      }
      u.failed_logins = 0;
      u.locked_until = null;
      state.session = u.id;
      persist();
      return me(u);
    },
    { public: true },
  );
  route('POST', '/auth/logout', () => {
    state.session = null;
    persist();
  }, { public: true });
  route('GET', '/me', (ctx) => me(user(ctx.userId)));
  route('GET', '/config', () => ({
    upload: { allowed_extensions: ALLOWED, max_batch_bytes: MAX_BATCH_BYTES },
    confidence_thresholds: { high: 0.8, medium: 0.5 },
  }));

  // --- Пакеты ---------------------------------------------------------------------------------

  route('GET', '/batches', (ctx) => {
    const q = ctx.query;
    const search = (q.get('search') ?? '').trim().toLowerCase();
    const status = q.get('status') ?? 'all';
    const level = q.get('level') ?? 'any';
    const period = q.get('period') ?? '30d';
    const owner = q.get('owner') ?? 'all';
    const page = Math.max(1, Number(q.get('page') ?? 1));
    const pageSize = Math.min(100, Math.max(1, Number(q.get('page_size') ?? 20)));
    const since = period === 'all' ? '' : new Date(Date.now() - (period === '7d' ? 7 : 30) * 86400_000).toISOString();
    const base = state.batches.filter(
      (b) => !b.deleted && b.status !== 'draft' && (owner !== 'me' || b.author_id === ctx.userId) && b.created_at >= since,
    );
    const matches = base.filter((b) => {
      if (status === 'processing' && !['queued', 'processing'].includes(b.status)) return false;
      if (status === 'done' && b.status !== 'done') return false;
      if (status === 'error' && b.status !== 'partial_error') return false;
      if (search) {
        const inName = b.name.toLowerCase().includes(search) || (b.subject ?? '').toLowerCase().includes(search);
        const inFiles = state.documents.some((d) => d.batch_id === b.id && d.filename.toLowerCase().includes(search));
        if (!inName && !inFiles) return false;
      }
      if (level !== 'any' && batchSummary(state, b).worst_level !== level) return false;
      return true;
    });
    matches.sort((a, b) => b.created_at.localeCompare(a.created_at));
    const items = matches.slice((page - 1) * pageSize, page * pageSize).map((b) => {
      const docs = state.documents.filter((d) => d.batch_id === b.id);
      const summary = batchSummary(state, b);
      const author = state.users.find((u) => u.id === b.author_id);
      const profile = state.profiles.find((p) => p.id === b.profile_id);
      return {
        id: b.id,
        name: b.name,
        subject: b.subject,
        created_at: b.created_at,
        author: author ? { id: author.id, full_name: author.full_name } : null,
        profile: profile ? { id: profile.id, short_name: profile.short_name } : null,
        documents_count: docs.length,
        status: b.status,
        error_documents_count: docs.filter((d) => d.status === 'error').length,
        worst_level: summary.worst_level,
        findings: summary.findings,
      };
    });
    return {
      items,
      total: matches.length,
      processing_count: base.filter((b) => ['queued', 'processing'].includes(b.status)).length,
      page,
      page_size: pageSize,
    };
  });

  route('POST', '/batches', (ctx) => {
    const batch: MBatch = {
      id: uid(), name: '', subject: null, meta: null, status: 'draft', profile_id: defaultProfile().id,
      author_id: ctx.userId, total_bytes: 0, created_at: nowIso(), finished_at: null, deleted: false, files: [],
    };
    state.batches.push(batch);
    persist();
    return [201, { id: batch.id, status: 'draft', name: '', total_bytes: 0, max_batch_bytes: MAX_BATCH_BYTES, files: [] }];
  });

  route('POST', '/batches/:id/files', async (ctx) => {
    const batch = getBatch(ctx.params.id);
    if (batch.status !== 'draft') throw new HttpError(409, 'conflict', 'Пакет уже запущен, файлы добавлять нельзя');
    const added = ctx.files.reduce((s, f) => s + f.size, 0);
    // Отправка файлов: прогресс растёт за время, пропорциональное объёму
    const steps = 6;
    for (let i = 1; i <= steps; i++) {
      await sleep(80 + Math.min(400, added / 400_000));
      ctx.onUploadProgress?.((added * i) / steps, added);
    }
    if (batch.total_bytes + added > MAX_BATCH_BYTES) {
      throw new HttpError(413, 'batch_too_large', 'Пакет больше 100 МБ', { total_bytes: batch.total_bytes, max_batch_bytes: MAX_BATCH_BYTES });
    }
    for (const file of ctx.files) {
      const checked = await checkFile(file);
      batch.files.push({ id: uid(), filename: file.name, type_label: ext(file.name).toUpperCase(), size_bytes: file.size, ...checked });
      if (!batch.name && ext(file.name) === 'zip') batch.name = file.name.replace(/\.zip$/i, '');
    }
    batch.total_bytes += added;
    persist();
    return [201, { files: batch.files, total_bytes: batch.total_bytes }];
  });

  route('DELETE', '/batches/:id/files/:fileId', (ctx) => {
    const batch = getBatch(ctx.params.id);
    if (batch.status !== 'draft') throw new HttpError(409, 'conflict', 'Пакет уже запущен, файлы удалять нельзя');
    const file = batch.files.find((f) => f.id === ctx.params.fileId);
    if (!file) throw notFound('Файл не найден');
    batch.files = batch.files.filter((f) => f !== file);
    batch.total_bytes = Math.max(0, batch.total_bytes - file.size_bytes);
    persist();
    return { files: batch.files, total_bytes: batch.total_bytes };
  });

  route('POST', '/batches/:id/start', (ctx) => {
    const batch = getBatch(ctx.params.id);
    if (batch.status !== 'draft') throw new HttpError(409, 'conflict', 'Пакет уже запущен');
    const checkable = batch.files.filter(
      (f) => f.check === 'ok' || f.check === 'encrypted' || (f.archive_entries ?? []).some((e) => e.check === 'ok'),
    );
    if (!checkable.length) throw validation({ files: 'Нет ни одного файла, который можно проверить' }, 'nothing_to_check');
    const name = String(ctx.body.name ?? '').trim();
    if (!name) throw validation({ name: 'Введите название пакета' });
    const profileId = String(ctx.body.profile_id ?? '') || batch.profile_id;
    if (!state.profiles.some((p) => p.id === profileId)) throw validation({ profile_id: 'Профиль правил не найден' });
    batch.name = name;
    batch.profile_id = profileId;
    batch.status = 'queued';
    batch.created_at = nowIso();
    for (const file of checkable) {
      const entries: { path: string; size: number; check: FileCheck }[] = file.archive_entries
        ? file.archive_entries.filter((e) => e.check === 'ok').map((e) => ({ path: e.path, size: e.size_bytes, check: e.check }))
        : [{ path: file.filename, size: file.size_bytes, check: file.check }];
      for (const entry of entries) {
        const filename = entry.path.split('/').pop() ?? entry.path;
        const templateName = templateNameFor(filename);
        state.documents.push({
          id: uid(), batch_id: batch.id, filename, original_path: entry.path, kind: ext(filename) === 'pdf' ? 'pdf' : (ext(filename) as MDocument['kind']),
          pages: null, size_bytes: entry.size, status: 'queued', stage: 'queued', stage_detail: null, progress: 0,
          error_code: null, error_message: null, ocr_confidence: null, low_quality_pages: [], worst_level: null,
          fail_with: entry.check === 'encrypted' ? 'encrypted' : null, template: templateName, include_rules: null, finished_at: null,
        });
      }
    }
    batch.files = [];
    persist();
    startTicker();
    return [202, { id: batch.id, status: batch.status }];
  });

  route('GET', '/batches/:id', (ctx) => batchView(state, getBatch(ctx.params.id)));

  route('PATCH', '/batches/:id', (ctx) => {
    const batch = getBatch(ctx.params.id);
    if (ctx.body.name !== undefined) {
      const name = String(ctx.body.name).trim();
      if (!name) throw validation({ name: 'Введите название пакета' });
      batch.name = name;
    }
    if (ctx.body.subject !== undefined) batch.subject = String(ctx.body.subject).trim() || null;
    persist();
    return batchView(state, batch);
  });

  route('DELETE', '/batches/:id', (ctx) => {
    const batch = getBatch(ctx.params.id);
    if (batch.status === 'draft') state.batches = state.batches.filter((b) => b !== batch);
    else batch.deleted = true;
    persist();
    return [204];
  });

  route('POST', '/batches/:id/rerun', (ctx) => {
    const batch = getBatch(ctx.params.id);
    if (['draft', 'queued', 'processing'].includes(batch.status)) throw new HttpError(409, 'conflict', 'Пакет ещё обрабатывается');
    for (const doc of state.documents.filter((d) => d.batch_id === batch.id && d.status === 'done')) {
      Object.assign(doc, { status: 'queued', stage: 'queued', progress: 0, stage_detail: null, worst_level: null });
      state.findings = state.findings.filter((f) => !(f.document_id === doc.id && f.source === 'ai'));
    }
    batch.status = 'queued';
    batch.finished_at = null;
    persist();
    startTicker();
    return [202];
  });

  route('GET', '/batches/:id/findings', (ctx) => {
    const batch = getBatch(ctx.params.id);
    const q = ctx.query;
    let rows = state.findings.filter((f) => f.batch_id === batch.id);
    if (q.get('level')) rows = rows.filter((f) => f.level === q.get('level'));
    if (q.get('rule_id')) rows = rows.filter((f) => f.rule_id === q.get('rule_id'));
    if (q.get('status')) rows = rows.filter((f) => f.status === q.get('status'));
    if (q.get('document_id')) rows = rows.filter((f) => f.document_id === q.get('document_id'));
    const page = Math.max(1, Number(q.get('page') ?? 1));
    const pageSize = Math.min(100, Math.max(1, Number(q.get('page_size') ?? 20)));
    const items = findingsPayload(state, rows.slice((page - 1) * pageSize, page * pageSize), ctx.userId).map((f) => {
      const doc = state.documents.find((d) => d.id === f.document_id);
      return { ...f, quote: f.quote.length <= 200 ? f.quote : `${f.quote.slice(0, 199)}…`, document: doc ? { id: doc.id, filename: doc.filename } : null };
    });
    return { items, total: rows.length, page, page_size: pageSize };
  });

  // --- Документы ------------------------------------------------------------------------------

  route('GET', '/documents', (ctx) => {
    const limit = Number(ctx.query.get('limit') ?? 20);
    return state.documents
      .filter((d) => d.status === 'done' && state.batches.some((b) => b.id === d.batch_id && !b.deleted))
      .sort((a, b) => (b.finished_at ?? '').localeCompare(a.finished_at ?? ''))
      .slice(0, limit)
      .map((d) => {
        const b = state.batches.find((x) => x.id === d.batch_id)!;
        return { id: d.id, filename: d.filename, batch: { id: b.id, name: b.name } };
      });
  });

  route('GET', '/documents/:id', (ctx) => documentView(state, getDoc(ctx.params.id), ctx.userId));

  route('GET', '/documents/:id/blocks', (ctx) => {
    const doc = getDoc(ctx.params.id);
    const from = Number(ctx.query.get('from_page'));
    const to = Number(ctx.query.get('to_page'));
    if (!(to >= from) || to - from + 1 > 20) throw validation({ to_page: 'Диапазон — от 1 до 20 страниц' });
    return {
      blocks: state.blocks
        .filter((b) => b.document_id === doc.id && b.page >= from && b.page <= to)
        .sort((a, b) => a.order - b.order)
        .map(stripBlock),
    };
  });

  route('GET', '/documents/:id/search', (ctx) => {
    const doc = getDoc(ctx.params.id);
    const fold = (s: string) => s.toLowerCase().replaceAll('ё', 'е');
    const q = fold((ctx.query.get('q') ?? '').trim());
    if (q.length < 2) throw validation({ q: 'Введите не меньше двух символов' });
    const matches = [];
    for (const block of state.blocks.filter((b) => b.document_id === doc.id).sort((a, b) => a.order - b.order)) {
      const text = fold(block.text);
      for (let i = text.indexOf(q); i >= 0; i = text.indexOf(q, i + q.length)) {
        matches.push({ block_id: block.id, start: i, end: i + q.length, page: block.page });
        if (matches.length >= 200) return { matches };
      }
    }
    return { matches };
  });

  route('POST', '/documents/:id/retry', (ctx) => {
    const doc = getDoc(ctx.params.id);
    if (doc.status !== 'error') throw new HttpError(409, 'conflict', 'Повторить можно только документ с ошибкой');
    Object.assign(doc, { status: 'queued', stage: 'queued', progress: 0, error_code: null, error_message: null, worst_level: null });
    const batch = getBatch(doc.batch_id);
    batch.status = 'processing';
    batch.finished_at = null;
    persist();
    emitDocProgress(doc);
    startTicker();
    return [202];
  });

  // --- Находки --------------------------------------------------------------------------------

  route('PATCH', '/findings/:id', (ctx) => {
    const f = getFinding(ctx.params.id);
    const body = ctx.body as { version?: number; status?: FindingStatus; level?: Level; explanation?: string; comment?: string };
    if (body.version !== f.version) {
      throw new HttpError(409, 'version_conflict', 'Объект уже изменил другой пользователь. Обновите страницу', {
        current: findingsPayload(state, [f], ctx.userId)[0],
      });
    }
    if (body.level !== undefined && !['critical', 'warning'].includes(body.level)) throw validation({ level: 'Уровень — critical или warning' });
    const old = snapshot(f);
    const edits = (body.level !== undefined && body.level !== f.level) || (body.explanation !== undefined && body.explanation !== f.explanation);
    const target: FindingStatus = body.status ?? (edits ? 'edited' : f.status);
    if (!['new', 'accepted', 'rejected', 'edited'].includes(target)) throw validation({ status: 'Неизвестный статус находки' });
    if (!canTransition(f.status, target)) throw new HttpError(409, 'invalid_transition', 'Такое изменение статуса недоступно');
    let action: HistoryAction = 'commented';
    if (target !== f.status) action = ({ accepted: 'accepted', rejected: 'rejected', edited: 'edited', new: 'reopened' } as const)[target];
    else if (edits) action = 'edited';
    if (target === 'new') {
      f.decided_by = null;
      f.decided_at = null;
      f.level = f.original_level;
    } else if (target !== f.status || edits) {
      f.decided_by = ctx.userId;
      f.decided_at = nowIso();
    }
    f.status = target;
    if (body.level !== undefined && target !== 'new') f.level = body.level;
    if (body.explanation !== undefined) f.explanation = body.explanation;
    if (body.comment !== undefined) f.comment = body.comment || null;
    f.version += 1;
    state.events.push({ id: uid(), finding_id: f.id, actor_id: ctx.userId, action, old, new: snapshot(f), created_at: nowIso() });
    persist();
    const doc = getDoc(f.document_id);
    return { finding: findingsPayload(state, [f], ctx.userId)[0], ...recalc(state, doc) };
  });

  route('POST', '/documents/:id/findings', (ctx) => {
    const doc = getDoc(ctx.params.id);
    const body = ctx.body as { block_id?: string; start?: number; end?: number; level?: Level; comment?: string };
    const block = state.blocks.find((b) => b.id === body.block_id);
    const fields: Record<string, string> = {};
    const start = Number(body.start);
    const end = Number(body.end);
    if (!block || block.document_id !== doc.id) fields.block_id = 'Фрагмент не найден в этом документе';
    else if (!(start >= 0 && start < end && end <= block.text.length)) fields.start = 'Выделение выходит за границы фрагмента';
    if (!body.level || !['critical', 'warning'].includes(body.level)) fields.level = 'Уровень — critical или warning';
    if (Object.keys(fields).length || !block) throw validation(fields);
    const comment = (body.comment ?? '').trim() || null;
    const f: MFinding = {
      id: uid(), batch_id: doc.batch_id, document_id: doc.id, rule_id: null, source: 'user', level: body.level!, original_level: body.level!,
      status: 'accepted', block_id: block.id, start, end, quote: block.text.slice(start, end), explanation: comment ?? '',
      recommendation: null, confidence: null, comment, author_id: ctx.userId, decided_by: ctx.userId, decided_at: nowIso(),
      version: 1, created_at: nowIso(),
    };
    state.findings.push(f);
    state.events.push({ id: uid(), finding_id: f.id, actor_id: ctx.userId, action: 'created', old: null, new: snapshot(f), created_at: nowIso() });
    persist();
    return [201, { finding: findingsPayload(state, [f], ctx.userId)[0], ...recalc(state, doc) }];
  });

  route('DELETE', '/findings/:id', (ctx) => {
    const f = getFinding(ctx.params.id);
    if (f.source !== 'user' || f.author_id !== ctx.userId) throw new HttpError(403, 'forbidden', 'Удалить можно только свою ручную пометку');
    state.findings = state.findings.filter((x) => x !== f);
    persist();
    recalc(state, getDoc(f.document_id));
    return [204];
  });

  route('GET', '/findings/:id/history', (ctx) => {
    const f = getFinding(ctx.params.id);
    const events = state.events
      .filter((e) => e.finding_id === f.id)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
      .map((e) => ({
        action: e.action,
        actor: { full_name: e.actor_id ? (state.users.find((u) => u.id === e.actor_id)?.full_name ?? '—') : 'ИИ' },
        old: e.old,
        new: e.new,
        created_at: e.created_at,
      }));
    if (f.source === 'ai') events.unshift({ action: 'created', actor: { full_name: 'ИИ' }, old: null, new: snapshot(f), created_at: f.created_at });
    return { events };
  });

  // --- Экспорт --------------------------------------------------------------------------------

  const createExport = (scope: 'document' | 'batch', targetId: string) => {
    const stem = scope === 'document' ? getDoc(targetId).filename.replace(/\.[^.]+$/, '') : getBatch(targetId).name || 'Пакет';
    const exp = { id: uid(), scope, target_id: targetId, filename: `${stem}_разметка_демо.txt`, ready_at: Date.now() + 1800 };
    state.exports.push(exp);
    persist();
    const batchId = scope === 'document' ? getDoc(targetId).batch_id : targetId;
    setTimeout(() => emit(batchId, { type: 'export.done', data: { ...exportStatus(exp.id), export_id: exp.id } }), 1900);
    return [202, { export_id: exp.id, format: 'txt' }];
  };
  route('POST', '/batches/:id/clarification', (ctx) => createExport('batch', getBatch(ctx.params.id).id));
  route('POST', '/batches/:id/ask', async (ctx) => {
    const batch = getBatch(ctx.params.id);
    const question = String(ctx.body.question ?? '').trim();
    if (!question) throw validation({ question: 'Напишите вопрос' });
    await new Promise((r) => setTimeout(r, 700));
    const words = question.toLowerCase().match(/[а-яё]{4,}/g)?.map((w) => w.slice(0, 5)) ?? [];
    const docs = state.documents.filter((d) => d.batch_id === batch.id);
    const scored = state.blocks
      .filter((b) => docs.some((d) => d.id === b.document_id))
      .map((b) => ({ b, score: words.filter((w) => b.text.toLowerCase().includes(w)).length }))
      .sort((x, y) => y.score - x.score);
    const best = scored[0];
    if (!best?.score) return { found: false, answer: 'В документах пакета ответа на этот вопрос нет.', citations: [] };
    const doc = docs.find((d) => d.id === best.b.document_id);
    const quote = best.b.text.slice(0, 200);
    return {
      found: true,
      answer: `Ответ мока API, модель не вызывалась: ${quote}`,
      citations: [{ document_id: best.b.document_id, filename: doc?.filename, block_id: best.b.id, start: 0, end: quote.length, quote, page: best.b.page, section_number: best.b.section_number ?? best.b.number }],
    };
  });
  route('POST', '/batches/:id/brief', (ctx) => {
    const batch = getBatch(ctx.params.id);
    const active = state.findings.filter((f) => f.batch_id === batch.id && f.status !== 'rejected');
    const decision = active.some((f) => f.level === 'critical') ? 'clarify' : active.length ? 'caution' : 'go';
    const brief = {
      decision,
      decision_label: { go: 'Существенных рисков не найдено', caution: 'Можно участвовать, учитывая риски', clarify: 'Сначала снять критичные риски' }[decision],
      summary: `Вывод мока API, модель не вызывалась: активных находок — ${active.length}.`,
      top_risks: [],
      actions: active.length ? ['Направить заказчику запрос о даче разъяснений по критичным условиям.'] : [],
      not_checked: 0,
    };
    batch.meta = { ...(batch.meta ?? {}), brief } as typeof batch.meta;
    persist();
    return brief;
  });
  route('POST', '/documents/:id/exports', (ctx) => createExport('document', getDoc(ctx.params.id).id));
  route('POST', '/batches/:id/exports', (ctx) => createExport('batch', getBatch(ctx.params.id).id));
  route('GET', '/exports/:id', (ctx) => exportStatus(ctx.params.id));

  function exportStatus(id: string) {
    const exp = state.exports.find((e) => e.id === id);
    if (!exp) throw notFound('Выгрузка не найдена');
    if (Date.now() < exp.ready_at) return { export_id: exp.id, status: 'processing' as const, download_url: null, filename: exp.filename, error_message: null };
    let url = exportUrls.get(exp.id);
    if (!url) {
      url = URL.createObjectURL(new Blob([exportText(exp.scope, exp.target_id)], { type: 'text/plain;charset=utf-8' }));
      exportUrls.set(exp.id, url);
    }
    return { export_id: exp.id, status: 'done' as const, download_url: url, filename: exp.filename, error_message: null };
  }

  function exportText(scope: 'document' | 'batch', targetId: string): string {
    const docs = scope === 'document' ? [getDoc(targetId)] : state.documents.filter((d) => d.batch_id === targetId);
    const levelLabel = { critical: 'Критично', warning: 'Внимание' };
    const lines = ['Демо-выгрузка мока API. Настоящий экспорт кладёт подсветку и комментарии в DOCX и PDF.', ''];
    for (const doc of docs) {
      lines.push(`Документ: ${doc.filename}`);
      const active = findingsPayload(state, state.findings.filter((f) => f.document_id === doc.id && f.status !== 'rejected'), null);
      if (!active.length) lines.push('  Рисков не найдено');
      for (const f of active.sort((a, b) => (a.order ?? 0) - (b.order ?? 0))) {
        const where = [f.section_number ? `п. ${f.section_number}` : null, f.page ? `стр. ${f.page}` : null].filter(Boolean).join(', ');
        lines.push(`  ${f.order}. [${levelLabel[f.level]}] ${f.rule ? `${f.rule.code} ${f.rule.title}` : 'Ручная пометка'} — ${where}`);
        lines.push(`     «${f.quote}»`);
        if (f.explanation) lines.push(`     ${f.explanation}`);
        if (f.comment) lines.push(`     Комментарий: ${f.comment}`);
      }
      lines.push('');
    }
    return lines.join('\n');
  }

  // --- Профили и правила ----------------------------------------------------------------------

  const profileView = (id: string) => {
    const p = state.profiles.find((x) => x.id === id)!;
    return {
      id: p.id, name: p.name, short_name: p.short_name, is_default: p.is_default,
      enabled_rules_count: state.rules.filter((r) => r.profile_id === p.id && r.enabled && !r.deleted).length,
    };
  };

  route('GET', '/rule-profiles', () =>
    [...state.profiles]
      .sort((a, b) => Number(b.is_default) - Number(a.is_default) || a.created_at.localeCompare(b.created_at))
      .map((p) => profileView(p.id)),
  );
  route('POST', '/rule-profiles', (ctx) => {
    requireMethodologist(ctx);
    const name = String(ctx.body.name ?? '').trim();
    const shortName = String(ctx.body.short_name ?? '').trim() || name;
    if (!name) throw validation({ name: 'Заполните название' });
    const profile = { id: uid(), name, short_name: shortName, is_default: false, created_at: nowIso() };
    state.profiles.push(profile);
    const from = ctx.body.copy_from_profile_id ? String(ctx.body.copy_from_profile_id) : null;
    if (from) {
      for (const r of state.rules.filter((x) => x.profile_id === from && !x.deleted)) {
        state.rules.push({ ...r, id: uid(), profile_id: profile.id, version: 1, updated_at: nowIso(), updated_by: ctx.userId, variables: r.variables.map((v) => ({ ...v })) });
      }
    }
    persist();
    return [201, profileView(profile.id)];
  });
  route('PATCH', '/rule-profiles/:id', (ctx) => {
    requireMethodologist(ctx);
    const profile = state.profiles.find((p) => p.id === ctx.params.id);
    if (!profile) throw notFound('Профиль не найден');
    if (ctx.body.name !== undefined) profile.name = String(ctx.body.name).trim() || profile.name;
    if (ctx.body.short_name !== undefined) profile.short_name = String(ctx.body.short_name).trim() || profile.short_name;
    if (ctx.body.is_default === true) for (const p of state.profiles) p.is_default = p === profile;
    persist();
    return profileView(profile.id);
  });
  route('DELETE', '/rule-profiles/:id', (ctx) => {
    requireMethodologist(ctx);
    const profile = state.profiles.find((p) => p.id === ctx.params.id);
    if (!profile) throw notFound('Профиль не найден');
    if (profile.is_default) throw new HttpError(409, 'default_profile', 'Профиль по умолчанию удалить нельзя');
    state.profiles = state.profiles.filter((p) => p !== profile);
    persist();
    return [204];
  });

  route('GET', '/rules/system-variables', () => [
    { name: 'фрагмент', type: 'auto', description: 'Найденный фрагмент документа' },
    { name: 'предмет_закупки', type: 'auto', description: 'Из извещения' },
  ]);
  route('GET', '/rules', (ctx) => {
    requireMethodologist(ctx);
    const profileId = ctx.query.get('profile_id') || defaultProfile().id;
    const search = (ctx.query.get('search') ?? '').trim().toLowerCase();
    const kind = ctx.query.get('kind');
    return state.rules
      .filter((r) => r.profile_id === profileId && !r.deleted)
      .filter((r) => !search || r.code.toLowerCase().includes(search) || r.title.toLowerCase().includes(search))
      .filter((r) => !kind || r.kind === kind)
      .sort((a, b) => a.code.localeCompare(b.code))
      .map((r) => ({ id: r.id, code: r.code, title: r.title, kind: r.kind, level: r.level, enabled: r.enabled, hits_30d: hits30d(state, r.id) }));
  });
  route('GET', '/rules/:id', (ctx) => {
    requireMethodologist(ctx);
    return ruleView(state, getRule(ctx.params.id));
  });
  route('POST', '/rules', (ctx) => {
    requireMethodologist(ctx);
    const data = normalizeRule(ctx.body);
    const profileId = data.profile_id || defaultProfile().id;
    const warnings = checkRule(profileId, data, null);
    const rule: MRule = { ...data, id: uid(), profile_id: profileId, version: 1, updated_at: nowIso(), updated_by: ctx.userId, deleted: false };
    state.rules.push(rule);
    persist();
    return [201, { ...ruleView(state, rule), warnings }];
  });
  route('PATCH', '/rules/:id', (ctx) => {
    requireMethodologist(ctx);
    const rule = getRule(ctx.params.id);
    if (ctx.body.version !== rule.version) {
      throw new HttpError(409, 'version_conflict', 'Правило уже изменил другой методолог. Обновите страницу', { current: ruleView(state, rule) });
    }
    const merged = normalizeRule({ ...rule, ...ctx.body });
    const warnings = checkRule(rule.profile_id, merged, rule.id);
    Object.assign(rule, { ...merged, profile_id: rule.profile_id, version: rule.version + 1, updated_at: nowIso(), updated_by: ctx.userId });
    persist();
    return { ...ruleView(state, rule), warnings };
  });
  route('DELETE', '/rules/:id', (ctx) => {
    requireMethodologist(ctx);
    const rule = getRule(ctx.params.id);
    rule.deleted = true;
    rule.enabled = false;
    persist();
    return [204];
  });
  route('POST', '/rules/draft', async (ctx) => {
    requireMethodologist(ctx);
    const description = String(ctx.body.description ?? '').trim();
    if (description.length < 10) throw validation({ description: 'Опишите риск хотя бы одним предложением' });
    await new Promise((r) => setTimeout(r, 500));
    return {
      code: 'R-09', title: description.slice(0, 80), description, level: 'warning', kind: 'semantic_llm',
      prompt_template: `Проверь {фрагмент}: найди условия о том, что описано так: ${description}. Риск — только если условие прямо это предусматривает. Не риск: условия о другом.`,
      variables: [], risk_example: description, safe_example: '', warnings: [],
    };
  });
  route('POST', '/rules/test', async (ctx) => {
    requireMethodologist(ctx);
    const started = Date.now();
    const rule = normalizeRule((ctx.body.rule ?? {}) as Record<string, unknown>);
    const check = validateRule(rule);
    if (Object.keys(check.fields).length) throw validation(check.fields);
    let blocks: { id: string; text: string; section_number?: string | null; page?: number | null }[];
    if (ctx.body.document_id) {
      const doc = getDoc(String(ctx.body.document_id));
      blocks = state.blocks.filter((b) => b.document_id === doc.id).sort((a, b) => a.order - b.order)
        .map((b) => ({ id: b.id, text: b.text, section_number: b.section_number ?? b.number, page: b.page }));
    } else {
      const text = String(ctx.body.text ?? '');
      if (!text.trim()) throw validation({ text: 'Вставьте текст для проверки' });
      if (text.length > 20000) throw validation({ text: 'Текст длиннее 20 000 символов' }, 'text_too_long');
      blocks = text.split(/\n+/).filter((x) => x.trim()).map((x, i) => ({ id: `text-${i}`, text: x, section_number: null, page: null }));
    }
    await sleep(1200 + Math.random() * 1300);
    return { findings: testRule(rule, blocks), duration_ms: Date.now() - started };
  });

  function normalizeRule(body: Record<string, unknown>): RuleInput {
    return {
      profile_id: String(body.profile_id ?? ''),
      code: String(body.code ?? '').trim(),
      title: String(body.title ?? '').trim(),
      description: String(body.description ?? '').trim(),
      level: body.level as Level,
      kind: body.kind as RuleInput['kind'],
      prompt_template: String(body.prompt_template ?? '').trim(),
      variables: Array.isArray(body.variables) ? (body.variables as RuleInput['variables']) : [],
      enabled: body.enabled === undefined ? true : Boolean(body.enabled),
    };
  }

  function checkRule(profileId: string, data: RuleInput, ruleId: string | null): string[] {
    const { fields, warnings } = validateRule(data);
    if (!['critical', 'warning'].includes(data.level)) fields.level = 'Уровень — «Критично» или «Внимание»';
    if (!['semantic_llm', 'llm'].includes(data.kind)) fields.kind = 'Тип правила — «Смысловой поиск + проверка ИИ» или «Проверка ИИ»';
    if (state.rules.some((r) => r.profile_id === profileId && r.code === data.code && !r.deleted && r.id !== ruleId)) {
      fields.code ??= `Правило с кодом ${data.code} уже есть в профиле`;
    }
    if (Object.keys(fields).length) throw validation(fields);
    if (data.kind === 'llm' && data.enabled) {
      const n = state.rules.filter((r) => r.profile_id === profileId && r.kind === 'llm' && r.enabled && !r.deleted && r.id !== ruleId).length;
      if (n >= 5) {
        throw validation({ kind: `В профиле уже ${n} правил «Проверка ИИ»: каждое читает документ целиком, больше 5 замедлят проверку` }, 'too_many_llm_rules');
      }
    }
    return warnings;
  }

  // --- Конвейер: симуляция обработки --------------------------------------------------------

  let ticker: ReturnType<typeof setInterval> | null = null;

  function startTicker() {
    ticker ??= setInterval(tick, TICK_MS);
  }

  function emitDocProgress(doc: MDocument) {
    emit(doc.batch_id, {
      type: 'document.progress',
      data: {
        document_id: doc.id, status: doc.status, stage: doc.stage, stage_label: stageLabel(doc), stage_detail: doc.stage_detail,
        next_stage_label: doc.status === 'processing' ? (NEXT_STAGE_LABELS[doc.stage] ?? null) : null, progress: doc.progress,
      },
    });
  }

  function finishDocument(doc: MDocument, batch: MBatch) {
    const template = TEMPLATES[doc.template]();
    const { blocks, keys } = buildBlocks(doc.id, template);
    state.blocks = state.blocks.filter((b) => b.document_id !== doc.id).concat(blocks);
    const userFindings = state.findings.filter((f) => f.document_id === doc.id && f.source === 'user');
    state.findings = state.findings.filter((f) => f.document_id !== doc.id).concat(userFindings, buildFindings(state, doc, batch, template, keys, { reviewed: false }));
    Object.assign(doc, {
      status: 'done', stage: 'done', progress: 100, stage_detail: null, pages: template.pages, finished_at: nowIso(),
      ocr_confidence: template.kind === 'scan' ? (template.ocrConfidence ?? null) : null,
      low_quality_pages: template.kind === 'scan' ? (template.lowQualityPages ?? []) : [],
    });
    const card = documentCard(state, doc);
    doc.worst_level = card.worst_level;
    emit(batch.id, {
      type: 'document.done',
      data: { document_id: doc.id, pages: doc.pages, worst_level: card.worst_level, findings: card.findings, review: card.review, ocr_low_quality_pages: doc.low_quality_pages },
    });
    if (doc.template === 'notice' && !batch.subject) {
      batch.subject = blocks.find((b) => b.text.startsWith('Объект закупки'))?.text.replace(/^Объект закупки:\s*/, '').replace(/\.$/, '') ?? null;
      if (batch.subject) batch.subject = batch.subject[0].toUpperCase() + batch.subject.slice(1);
      emit(batch.id, { type: 'batch.meta', data: { subject: batch.subject } });
    }
  }

  function updatePassport(batch: MBatch) {
    const docs = new Map<TemplateName, { doc: MDocument; keys: Map<string, MBlock> }>();
    for (const doc of state.documents.filter((d) => d.batch_id === batch.id && d.status === 'done')) {
      if (docs.has(doc.template)) continue;
      const keys = new Map<string, MBlock>();
      const template = TEMPLATES[doc.template]();
      template.blocks.forEach((tb, i) => {
        const block = state.blocks.find((b) => b.id === `${doc.id}-b${i}`);
        if (tb.key && block) keys.set(tb.key, block);
      });
      docs.set(doc.template, { doc, keys });
    }
    const fields = passportFields(docs);
    batch.meta = fields.length ? { passport: { fields } } : batch.meta;
  }

  function tick() {
    let busy = false;
    for (const batch of state.batches.filter((b) => !b.deleted && (b.status === 'queued' || b.status === 'processing'))) {
      busy = true;
      const docs = state.documents.filter((d) => d.batch_id === batch.id);
      const rulesTotal = state.rules.filter((r) => r.profile_id === batch.profile_id && r.enabled && !r.deleted).length || 1;
      let changed = false;
      for (const doc of docs) {
        if (doc.status === 'done' || doc.status === 'error') continue;
        changed = true;
        if (batch.status === 'queued') batch.status = 'processing';
        const template = TEMPLATES[doc.template]();
        if (doc.stage === 'queued') {
          Object.assign(doc, { status: 'processing', stage: 'extracting', progress: 5, stage_detail: null });
        } else if (doc.stage === 'extracting') {
          doc.progress += 9;
          if (doc.fail_with && doc.progress >= 20) {
            Object.assign(doc, { status: 'error', stage: 'error', error_code: doc.fail_with, error_message: ERROR_MESSAGES[doc.fail_with] ?? 'Не удалось обработать файл', finished_at: nowIso() });
            emit(batch.id, { type: 'document.error', data: { document_id: doc.id, error: { code: doc.error_code, message: doc.error_message }, retryable: true } });
            continue;
          }
          if (doc.progress >= 30) {
            doc.pages = template.pages;
            if (template.kind === 'scan') Object.assign(doc, { kind: 'scan', stage: 'ocr', progress: 30, stage_detail: { page: 0, pages: template.pages } });
            else Object.assign(doc, { stage: 'analysis', progress: 70, stage_detail: { rules_done: 0, rules_total: rulesTotal } });
          }
        } else if (doc.stage === 'ocr') {
          const pages = doc.stage_detail?.pages ?? template.pages;
          const page = Math.min(pages, (doc.stage_detail?.page ?? 0) + 1);
          doc.stage_detail = { page, pages };
          doc.progress = 30 + Math.round((40 * page) / pages);
          if (page >= pages) Object.assign(doc, { stage: 'analysis', progress: 70, stage_detail: { rules_done: 0, rules_total: rulesTotal } });
        } else if (doc.stage === 'analysis') {
          const total = doc.stage_detail?.rules_total ?? rulesTotal;
          const done = Math.min(total, (doc.stage_detail?.rules_done ?? 0) + 1);
          doc.stage_detail = { rules_done: done, rules_total: total };
          doc.progress = 70 + Math.round((30 * done) / total);
          if (done >= total) {
            finishDocument(doc, batch);
            continue;
          }
        }
        emitDocProgress(doc);
      }
      const finished = docs.every((d) => d.status === 'done' || d.status === 'error');
      if (finished && docs.length) {
        batch.status = docs.every((d) => d.status === 'done') ? 'done' : 'partial_error';
        batch.finished_at = nowIso();
        updatePassport(batch);
        const summary = batchSummary(state, batch);
        emit(batch.id, { type: 'batch.done', data: { status: batch.status, worst_level: summary.worst_level, counters: summary.counters, review: summary.review } });
      } else if (changed) {
        const view = batchView(state, batch);
        emit(batch.id, { type: 'batch.progress', data: { status: batch.status, progress: view.progress!, eta_seconds: view.eta_seconds ?? null } });
      }
    }
    persist();
    if (!busy && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  }

  if (state.batches.some((b) => b.status === 'queued' || b.status === 'processing')) startTicker();

  // Для демо: window.__resetMockApi() возвращает исходные данные
  (window as unknown as Record<string, unknown>).__resetMockApi = () => {
    resetState();
    state = loadState();
    persist();
    location.reload();
  };

  return {
    async handle(req: TransportRequest, onUploadProgress): Promise<TransportResponse> {
      await latency();
      const routeMatch = routes
        .filter((r) => r.method === req.method)
        .map((r) => ({ r, m: r.pattern.exec(req.path) }))
        .find((x) => x.m);
      if (!routeMatch) return { status: 404, body: { error: { code: 'not_found', message: 'Не найдено', details: {} } } };
      const { r, m } = routeMatch;
      if (!r.public && !state.session) {
        return { status: 401, body: { error: { code: 'unauthorized', message: 'Сессия завершена, войдите снова', details: {} } } };
      }
      const params = Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m![i + 1])]));
      try {
        const result = await r.handler({
          params,
          query: req.query,
          body: (req.body ?? {}) as Record<string, unknown>,
          files: req.files ?? [],
          userId: state.session ?? '',
          onUploadProgress,
        });
        if (Array.isArray(result) && typeof result[0] === 'number') {
          return { status: result[0], body: structuredClone(result[1]) };
        }
        return { status: result === undefined ? 204 : 200, body: structuredClone(result) };
      } catch (e) {
        if (e instanceof HttpError) return { status: e.status, body: { error: { code: e.code, message: e.message, details: e.details } } };
        console.error('[mock api]', e);
        return { status: 500, body: { error: { code: 'internal', message: 'Ошибка на сервере. Повторите через минуту', details: {} } } };
      }
    },

    subscribe(batchId, listener) {
      const set = listeners.get(batchId) ?? new Set();
      set.add(listener);
      listeners.set(batchId, set);
      setTimeout(() => {
        const batch = state.batches.find((b) => b.id === batchId && !b.deleted);
        if (batch && set.has(listener)) listener({ type: 'batch.snapshot', data: structuredClone(batchView(state, batch)) });
      }, 50);
      return () => set.delete(listener);
    },
  };
}

// --- Вспомогательное ------------------------------------------------------------------------

function ext(filename: string): string {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i + 1).toLowerCase() : '';
}

function snapshot(f: MFinding) {
  return { status: f.status, level: f.level, explanation: f.explanation, comment: f.comment };
}

const CHECK_MESSAGES: Record<FileCheck, string | null> = {
  ok: null,
  unsupported: 'Формат не поддерживается — файл не будет проверен',
  encrypted: 'Файл защищён паролем — проверить его, скорее всего, не получится',
  corrupted: 'Файл повреждён и не открывается — он не будет проверен',
};

/** Проверка файла без извлечения текста, как `ml.check_file`: формат, пароль, повреждение. */
async function checkFile(file: File): Promise<Pick<UploadedFile, 'check' | 'message' | 'archive_entries'>> {
  const kind = ext(file.name);
  const result = (check: FileCheck, archive_entries: ArchiveEntry[] | null = null) => ({ check, message: CHECK_MESSAGES[check], archive_entries });
  if (!ALLOWED.includes(kind)) return result('unsupported');
  const head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
  const startsWith = (...bytes: number[]) => bytes.every((b, i) => head[i] === b);
  const isZip = startsWith(0x50, 0x4b);
  const isOle = startsWith(0xd0, 0xcf, 0x11, 0xe0);
  if (kind === 'pdf') {
    if (!startsWith(0x25, 0x50, 0x44, 0x46)) return result('corrupted');
    const tail = await file.slice(Math.max(0, file.size - 64 * 1024)).text();
    return result(tail.includes('/Encrypt') ? 'encrypted' : 'ok');
  }
  if (kind === 'docx') return result(isZip ? 'ok' : isOle ? 'encrypted' : 'corrupted');
  if (kind === 'doc') return result(isOle ? 'ok' : 'corrupted');
  if (kind === 'rtf') return result(startsWith(0x7b, 0x5c, 0x72, 0x74, 0x66) ? 'ok' : 'corrupted');
  if (kind === 'zip') {
    if (!isZip) return result('corrupted');
    const entries = await zipEntries(file);
    return entries ? result('ok', entries) : result('corrupted');
  }
  return result('ok');
}

/** Оглавление zip без распаковки: центральный каталог в конце архива. */
async function zipEntries(file: File): Promise<ArchiveEntry[] | null> {
  const tailSize = Math.min(file.size, 66_000);
  const tail = new DataView(await file.slice(file.size - tailSize).arrayBuffer());
  let eocd = -1;
  for (let i = tailSize - 22; i >= 0; i--) {
    if (tail.getUint32(i, true) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) return null;
  const count = tail.getUint16(eocd + 10, true);
  const cdSize = tail.getUint32(eocd + 12, true);
  const cdOffset = tail.getUint32(eocd + 16, true);
  const cd = new DataView(await file.slice(cdOffset, cdOffset + cdSize).arrayBuffer());
  const entries: ArchiveEntry[] = [];
  let pos = 0;
  for (let n = 0; n < count && pos + 46 <= cd.byteLength; n++) {
    if (cd.getUint32(pos, true) !== 0x02014b50) return null;
    const flags = cd.getUint16(pos + 8, true);
    const size = cd.getUint32(pos + 24, true);
    const nameLen = cd.getUint16(pos + 28, true);
    const extraLen = cd.getUint16(pos + 30, true);
    const commentLen = cd.getUint16(pos + 32, true);
    const nameBytes = new Uint8Array(cd.buffer, cd.byteOffset + pos + 46, nameLen);
    const name = new TextDecoder(flags & 0x800 ? 'utf-8' : 'ibm866').decode(nameBytes);
    pos += 46 + nameLen + extraLen + commentLen;
    if (name.endsWith('/') || name.startsWith('__MACOSX/')) continue;
    const unsafe = name.startsWith('/') || name.split('/').includes('..');
    const kind = ext(name);
    entries.push({ path: name, size_bytes: size, check: unsafe || !(ALLOWED.includes(kind) && kind !== 'zip') ? 'unsupported' : 'ok' });
  }
  return entries;
}

// --- Тест правила: эвристики вместо LLM -----------------------------------------------------

const RULE_PATTERNS: Record<string, { re: RegExp; reason: (m: RegExpMatchArray, vars: Record<string, number>) => string | null }> = {
  'R-01': { re: /(Intel|AMD|Xeon|Huawei|Cisco|Dell|Lenovo|HP)\b[^,.;]*/, reason: () => 'указана модель без слов «или эквивалент»' },
  'R-02': {
    re: /обеспечени[ея] исполнения[^.]*?(\d+)\s*(?:\([^)]*\)\s*)?(?:процент|%)/i,
    reason: (m, v) => (Number(m[1]) > (v['порог'] ?? 10) ? `обеспечение ${m[1]} % больше порога ${v['порог'] ?? 10} %` : null),
  },
  'R-03': { re: /(пен[июя]|штраф)[^.]*процент[^.]*/i, reason: () => 'ответственность в процентах от цены' },
  'R-04': {
    re: /в течение (\d+)\s*(?:\([^)]*\)\s*)?календарн\S* дн/i,
    reason: (m, v) => (Number(m[1]) < (v['порог'] ?? 10) ? `срок ${m[1]} дн. меньше порога ${v['порог'] ?? 10} дней` : null),
  },
  'R-05': { re: /одностороннем отказе[^.]*(любого|однократн)[^.]*/i, reason: () => 'отказ при любом, даже однократном нарушении' },
  'R-06': { re: /(лиценз|СРО|опыт исполнения|партн[её]р)[^.]*/i, reason: () => 'требование к участнику' },
  'R-07': {
    re: /(при[её]мка|оплата)[^.]*?в течение (\d+)\s*(?:\([^)]*\)\s*)?(рабоч|календарн)?/i,
    reason: (m) => `срок ${m[2]} дн. — проверьте по ст. 34 44-ФЗ`,
  },
  'R-08': {
    re: /гарантийный срок[^.]*?(\d+)\s*(?:\([^)]*\)\s*)?месяц/i,
    reason: (m, v) => (Number(m[1]) > (v['порог'] ?? 36) ? `гарантия ${m[1]} мес. больше порога ${v['порог'] ?? 36} мес.` : null),
  },
};

function testRule(rule: RuleInput, blocks: { id: string; text: string; section_number?: string | null; page?: number | null }[]): RuleTestFinding[] {
  const vars = Object.fromEntries(rule.variables.filter((v) => v.type === 'number').map((v) => [v.name, Number(v.value.replace(',', '.'))]));
  const pattern = RULE_PATTERNS[rule.code];
  const stems = [...`${rule.title} ${rule.description}`.toLowerCase().matchAll(/[а-яё]{6,}/g)].map((m) => m[0].slice(0, 5));
  const out: RuleTestFinding[] = [];
  for (const block of blocks) {
    for (const sentence of sentences(block.text)) {
      let reason: string | null = null;
      if (pattern) {
        const m = sentence.text.match(pattern.re);
        reason = m ? pattern.reason(m, vars) : null;
      } else {
        const lower = sentence.text.toLowerCase();
        const hits = new Set(stems.filter((s) => lower.includes(s)));
        reason = hits.size >= 3 ? 'фрагмент похож на описание правила' : null;
      }
      if (!reason) continue;
      out.push({
        quote: sentence.text, block_id: block.id, start: sentence.start, end: sentence.start + sentence.text.length,
        section_number: block.section_number ?? null, page: block.page ?? null, level: rule.level,
        explanation: `${rule.description.split(/(?<=[.:])\s/)[0].replace(/:$/, '.')} В этом фрагменте: ${reason}.`,
        confidence_level: pattern ? 'high' : 'low',
      });
      if (out.length >= 5) return out;
    }
  }
  return out;
}

function sentences(text: string): { text: string; start: number }[] {
  const out = [];
  const re = /[^.!?]+[.!?]*/g;
  for (const m of text.matchAll(re)) {
    const lead = m[0].length - m[0].trimStart().length;
    const value = m[0].trim();
    if (value.length > 10) out.push({ text: value, start: (m.index ?? 0) + lead });
  }
  return out;
}

export type { MockState };
