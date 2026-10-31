// Ответы мока в формате слоя данных (db/batches.py, db/documents.py, db/findings.py, db/rules.py):
// те же производные значения и та же сортировка, что у настоящего API.

import type {
  BatchView,
  DocumentCard,
  DocumentView,
  Finding,
  FindingOriginal,
  FindingStatus,
  Level,
  Recalc,
  Rule,
  WorstLevel,
} from '../api/types';
import type { MBatch, MDocument, MFinding, MockState, MRule, MUser } from './state';

export const PAGE_VIEW_KINDS: string[] = ['pdf', 'scan'];
const SEVERITY: Record<string, number> = { critical: 2, warning: 1 };
const DOCUMENT_ORDER: Record<string, number> = { critical: 0, warning: 1, ok: 2, null: 3, not_checked: 4 };

export const TRANSITIONS: Record<FindingStatus, FindingStatus[]> = {
  new: ['accepted', 'rejected', 'edited'],
  accepted: ['new'],
  rejected: ['new'],
  edited: ['new', 'accepted', 'rejected'],
};

export const STAGE_LABELS: Record<string, string> = {
  queued: 'В очереди',
  extracting: 'Извлечение текста',
  ocr: 'Распознавание текста',
  analysis: 'Анализ по правилам',
  done: 'Готово',
  error: 'Ошибка',
};

export const NEXT_STAGE_LABELS: Record<string, string | null> = {
  queued: 'Далее: извлечение текста',
  extracting: 'Далее: анализ по правилам',
  ocr: 'Далее: анализ по правилам',
  analysis: null,
  done: null,
  error: null,
};

export function stageLabel(doc: Pick<MDocument, 'stage' | 'stage_detail'>): string {
  const label = STAGE_LABELS[doc.stage] ?? doc.stage;
  const d = doc.stage_detail;
  if (d && doc.stage === 'ocr' && d.pages) return `${label}, стр. ${d.page ?? 0} из ${d.pages}`;
  if (d && doc.stage === 'analysis' && d.rules_total) return `${label}, ${d.rules_done ?? 0} из ${d.rules_total}`;
  return label;
}

export function canTransition(from: FindingStatus, to: FindingStatus): boolean {
  return from === to || TRANSITIONS[from].includes(to);
}

export function confidenceLevel(confidence: number | null) {
  if (confidence === null) return null;
  if (confidence >= 0.8) return 'high' as const;
  if (confidence >= 0.5) return 'medium' as const;
  return 'low' as const;
}

export function initials(fullName: string): string {
  return fullName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p[0].toUpperCase())
    .join('');
}

interface Counts {
  critical: number;
  warning: number;
  checked: number;
  total: number;
}

function counts(state: MockState, documentId: string): Counts {
  const c: Counts = { critical: 0, warning: 0, checked: 0, total: 0 };
  for (const f of state.findings) {
    if (f.document_id !== documentId) continue;
    if (f.status !== 'rejected') c[f.level] += 1;
    if (f.source === 'ai') {
      c.total += 1;
      if (f.status !== 'new') c.checked += 1;
    }
  }
  return c;
}

export function documentWorstLevel(state: MockState, doc: MDocument): WorstLevel {
  if (doc.status === 'error') return 'not_checked';
  if (doc.status !== 'done') return null;
  const c = counts(state, doc.id);
  if (c.critical) return 'critical';
  if (c.warning) return 'warning';
  return 'ok';
}

function batchDocs(state: MockState, batchId: string) {
  return state.documents.filter((d) => d.batch_id === batchId);
}

export function batchSummary(state: MockState, batch: MBatch) {
  const docs = batchDocs(state, batch.id);
  const counters = { total: docs.length, ok: 0, warning: 0, critical: 0, errors: 0 };
  const findings = { critical: 0, warning: 0 };
  const review = { checked: 0, total: 0 };
  let worst: WorstLevel = null;
  const levels: string[] = [];
  for (const doc of docs) {
    const level = documentWorstLevel(state, doc);
    const c = counts(state, doc.id);
    findings.critical += c.critical;
    findings.warning += c.warning;
    review.checked += c.checked;
    review.total += c.total;
    if (doc.status === 'error') counters.errors += 1;
    else if (doc.status === 'done' && level && level in counters) counters[level as 'ok' | 'warning' | 'critical'] += 1;
    if (doc.status === 'done' && level) levels.push(level);
  }
  if (!['draft', 'queued', 'processing'].includes(batch.status)) {
    worst = levels.length ? (levels.reduce((a, b) => ((SEVERITY[b] ?? 0) > (SEVERITY[a] ?? 0) ? b : a)) as WorstLevel) : 'not_checked';
  }
  return { worst_level: worst, counters, review, findings };
}

export function documentCard(state: MockState, doc: MDocument): DocumentCard {
  const c = counts(state, doc.id);
  return {
    id: doc.id,
    filename: doc.filename,
    original_path: doc.original_path,
    kind: doc.kind,
    pages: doc.pages,
    size_bytes: doc.size_bytes,
    status: doc.status,
    stage: doc.stage,
    stage_label: stageLabel(doc),
    stage_detail: doc.stage_detail,
    next_stage_label: doc.status === 'processing' ? (NEXT_STAGE_LABELS[doc.stage] ?? null) : null,
    progress: doc.progress,
    worst_level: documentWorstLevel(state, doc),
    findings: { critical: c.critical, warning: c.warning },
    review: { checked: c.checked, total: c.total },
    ocr_confidence: doc.ocr_confidence,
    ocr_low_quality_pages: doc.low_quality_pages,
    error: doc.status === 'error' ? { code: doc.error_code, message: doc.error_message } : null,
  };
}

function person(state: MockState, id: string | null) {
  const u = id ? state.users.find((x) => x.id === id) : undefined;
  return u ? { id: u.id, full_name: u.full_name } : null;
}

function eta(docs: MDocument[]): number | null {
  let remaining = 0;
  for (const doc of docs) {
    if (doc.status === 'done' || doc.status === 'error') continue;
    const pages = doc.pages ?? Math.max(1, Math.floor(doc.size_bytes / 60_000));
    const left = pages * (1 - doc.progress / 100);
    remaining += left * 0.6 + (doc.kind === 'scan' ? left * 0.4 : 0);
  }
  return remaining ? Math.max(5, Math.round(remaining / Math.max(1, Math.min(docs.length, 4)))) : null;
}

export function batchView(state: MockState, batch: MBatch): BatchView {
  const author = person(state, batch.author_id);
  const profile = state.profiles.find((p) => p.id === batch.profile_id);
  const base = {
    id: batch.id,
    name: batch.name,
    subject: batch.subject,
    meta: batch.meta,
    status: batch.status,
    created_at: batch.created_at,
    author,
    profile: profile ? { id: profile.id, name: profile.name, short_name: profile.short_name } : null,
  };
  if (batch.status === 'draft') {
    return { ...base, files: batch.files, total_bytes: batch.total_bytes, max_batch_bytes: 100 * 1024 * 1024, documents: [] };
  }
  const docs = batchDocs(state, batch.id);
  const summary = batchSummary(state, batch);
  const cards = docs
    .map((d) => documentCard(state, d))
    .sort(
      (a, b) =>
        (DOCUMENT_ORDER[String(a.worst_level)] ?? 3) - (DOCUMENT_ORDER[String(b.worst_level)] ?? 3) ||
        a.filename.localeCompare(b.filename, 'ru'),
    );
  const triggered = new Map<string, { rule: MRule; count: number }>();
  for (const f of state.findings) {
    if (f.batch_id !== batch.id || f.status === 'rejected' || !f.rule_id) continue;
    const rule = state.rules.find((r) => r.id === f.rule_id);
    if (!rule) continue;
    const entry = triggered.get(rule.id) ?? { rule, count: 0 };
    entry.count += 1;
    triggered.set(rule.id, entry);
  }
  const finished = docs.filter((d) => d.status === 'done' || d.status === 'error').length;
  return {
    ...base,
    progress: {
      done: finished,
      total: docs.length,
      errors: summary.counters.errors,
      percent: docs.length ? Math.round(docs.reduce((s, d) => s + d.progress, 0) / docs.length) : 0,
    },
    eta_seconds: batch.status === 'queued' || batch.status === 'processing' ? eta(docs) : null,
    worst_level: summary.worst_level,
    counters: summary.counters,
    review: summary.review,
    findings: summary.findings,
    documents: cards,
    rules_triggered: [...triggered.values()]
      .sort((a, b) => a.rule.code.localeCompare(b.rule.code))
      .map(({ rule, count }) => ({ rule_id: rule.id, code: rule.code, title: rule.title, level: rule.level, count })),
  };
}

export function findingsPayload(state: MockState, findings: MFinding[], viewerId: string | null): Finding[] {
  const order = new Map<string, number>();
  const docIds = new Set(findings.map((f) => f.document_id));
  for (const docId of docIds) {
    const blocks = new Map(state.blocks.filter((b) => b.document_id === docId).map((b) => [b.id, b.order]));
    state.findings
      .filter((f) => f.document_id === docId && f.status !== 'rejected')
      .sort((a, b) => (blocks.get(a.block_id) ?? 0) - (blocks.get(b.block_id) ?? 0) || a.start - b.start || a.created_at.localeCompare(b.created_at))
      .forEach((f, i) => order.set(f.id, i + 1));
  }
  return findings.map((f) => {
    const block = state.blocks.find((b) => b.id === f.block_id);
    const section = block?.section_block_id ? state.blocks.find((b) => b.id === block.section_block_id) : undefined;
    const rule = f.rule_id ? state.rules.find((r) => r.id === f.rule_id) : undefined;
    return {
      id: f.id,
      document_id: f.document_id,
      order: order.get(f.id) ?? null,
      source: f.source,
      status: f.status,
      level: f.level,
      original_level: f.original_level,
      rule: rule ? { id: rule.id, code: rule.code, title: rule.title } : null,
      source_available: PAGE_VIEW_KINDS.includes(state.documents.find((d) => d.id === f.document_id)?.kind ?? ''),
      block_id: f.block_id,
      start: f.start,
      end: f.end,
      quote: f.quote,
      section_block_id: block?.section_block_id ?? null,
      section_number: block ? (block.section_number ?? block.number?.replace(/\.$/, '') ?? null) : null,
      section_title: section?.text ?? null,
      page: block?.page ?? null,
      explanation: f.explanation,
      recommendation: f.recommendation,
      confidence: f.confidence,
      confidence_level: confidenceLevel(f.confidence),
      comment: f.comment,
      author: person(state, f.author_id),
      decided_by: person(state, f.decided_by),
      decided_at: f.decided_at,
      can_delete: f.source === 'user' && viewerId !== null && f.author_id === viewerId,
      version: f.version,
      created_at: f.created_at,
    };
  });
}

export function documentView(state: MockState, doc: MDocument, viewerId: string | null): DocumentView {
  const batch = state.batches.find((b) => b.id === doc.batch_id)!;
  const card = documentCard(state, doc);
  const out: DocumentView = {
    id: doc.id,
    batch: { id: batch.id, name: batch.name, subject: batch.subject },
    filename: doc.filename,
    kind: doc.kind,
    pages: doc.pages,
    status: doc.status,
    stage: doc.stage,
    stage_label: card.stage_label,
    progress: doc.progress,
    worst_level: card.worst_level,
    findings_summary: card.findings,
    review: card.review,
    ocr_confidence: doc.ocr_confidence,
    ocr_low_quality_pages: doc.low_quality_pages,
    error: card.error,
    blocks_complete: true,
    blocks: [],
    findings: [],
  };
  if (doc.status !== 'done') return out;
  let blocks = state.blocks.filter((b) => b.document_id === doc.id);
  if ((doc.pages ?? 0) > 100) {
    blocks = blocks.filter((b) => b.page <= 20);
    out.blocks_complete = false;
  }
  out.blocks = blocks.sort((a, b) => a.order - b.order).map(stripBlock);
  const payload = findingsPayload(state, state.findings.filter((f) => f.document_id === doc.id), viewerId);
  out.findings = payload.sort((a, b) => (a.order === null ? 1 : 0) - (b.order === null ? 1 : 0) || (a.order ?? 0) - (b.order ?? 0) || (a.page ?? 0) - (b.page ?? 0));
  return out;
}

export function stripBlock<T extends { document_id: string }>(block: T): Omit<T, 'document_id'> {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { document_id, ...rest } = block;
  return rest;
}

export function recalc(state: MockState, doc: MDocument): Recalc {
  const card = documentCard(state, doc);
  doc.worst_level = card.worst_level;
  const batch = state.batches.find((b) => b.id === doc.batch_id)!;
  return {
    document: { id: doc.id, worst_level: card.worst_level, findings_summary: card.findings, review: card.review },
    batch: { id: batch.id, ...batchSummary(state, batch) },
  };
}

function inLiveBatch(state: MockState, f: MFinding): boolean {
  return state.batches.some((b) => b.id === f.batch_id && !b.deleted);
}

export function hits30d(state: MockState, ruleId: string): number {
  const since = new Date(Date.now() - 30 * 24 * 3600_000).toISOString();
  return state.findings.filter((f) => f.rule_id === ruleId && f.source === 'ai' && f.created_at >= since && inLiveBatch(state, f)).length;
}

const ORIGINAL = { width: 595, margin: 56, line: 15, chars: 78, charWidth: 6, fontSize: 10 };

/** Страница оригинала в моке: текст блоков страницы картинкой SVG, рамки цитаты — доли её размера, как у API. */
export function findingOriginal(state: MockState, f: MFinding): FindingOriginal {
  const { width, margin, line, chars, charWidth, fontSize } = ORIGINAL;
  const target = state.blocks.find((b) => b.id === f.block_id)!;
  const blocks = state.blocks.filter((b) => b.document_id === f.document_id && b.page === target.page).sort((a, b) => a.order - b.order);
  const texts: string[] = [];
  const marks: { x0: number; y0: number; x1: number; y1: number }[] = [];
  let y = margin;
  for (const block of blocks) {
    for (let at = 0; at < block.text.length; at += chars) {
      const piece = block.text.slice(at, at + chars);
      const escaped = piece.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
      texts.push(`<text x="${margin}" y="${y}" textLength="${piece.length * charWidth}" font-size="${fontSize}" font-family="monospace">${escaped}</text>`);
      if (block.id === target.id && f.start < at + piece.length && f.end > at) {
        marks.push({
          x0: margin + (Math.max(f.start, at) - at) * charWidth,
          y0: y - line + 3,
          x1: margin + (Math.min(f.end, at + piece.length) - at) * charWidth,
          y1: y + 3,
        });
      }
      y += line;
    }
    y += line / 2;
  }
  const height = Math.max(842, y + margin);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}"><rect width="${width}" height="${height}" fill="white"/>${texts.join('')}</svg>`;
  return {
    page: target.page,
    image_url: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`,
    aspect: width / height,
    rects: marks.map((m) => ({ x0: m.x0 / width, y0: m.y0 / height, x1: m.x1 / width, y1: m.y1 / height })),
  };
}

export function ruleReview(state: MockState, ruleId: string) {
  const own = state.findings.filter((f) => f.rule_id === ruleId && f.source === 'ai' && inLiveBatch(state, f));
  const confirmed = own.filter((f) => f.status === 'accepted' || f.status === 'edited').length;
  const rejected = own.filter((f) => f.status === 'rejected').length;
  const decided = confirmed + rejected;
  return { confirmed, rejected, precision: decided ? Math.round((confirmed / decided) * 100) / 100 : null, noisy: decided >= 5 && rejected > confirmed };
}

export function ruleView(state: MockState, rule: MRule): Rule {
  const user = rule.updated_by ? state.users.find((u) => u.id === rule.updated_by) : undefined;
  return {
    id: rule.id,
    profile_id: rule.profile_id,
    code: rule.code,
    title: rule.title,
    description: rule.description,
    level: rule.level,
    kind: rule.kind,
    prompt_template: rule.prompt_template,
    variables: rule.variables,
    enabled: rule.enabled,
    hits_30d: hits30d(state, rule.id),
    review: ruleReview(state, rule.id),
    updated_at: rule.updated_at,
    updated_by: user ? { full_name: user.full_name } : null,
    version: rule.version,
  };
}

export function me(user: MUser) {
  const labels = { specialist: 'Специалист', lawyer: 'Юрист', methodologist: 'Методолог' } as const;
  return {
    id: user.id,
    full_name: user.full_name,
    initials: initials(user.full_name),
    role: user.role,
    role_label: labels[user.role],
    email: user.email,
  };
}

export const LEVELS: Level[] = ['critical', 'warning'];
