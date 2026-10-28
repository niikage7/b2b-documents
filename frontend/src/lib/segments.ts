// Разбиение текста блока на отрезки для подсветки. Смещения — в символах текста блока,
// с 0, `end` не включается (как в API). Отрезки не пересекаются и покрывают весь текст.

import type { Level } from '../api/types';

export interface MarkRange {
  start: number;
  end: number;
  /** Находка: id и уровень. Для поиска и временной подсветки — без id. */
  findingId?: string;
  level?: Level;
  rejected?: boolean;
  kind: 'finding' | 'search' | 'search-active' | 'pending' | 'flash';
}

export interface Segment {
  start: number;
  end: number;
  text: string;
  findingIds: string[];
  /** Худший уровень среди активных находок отрезка; null — только отклонённые или нет находок. */
  level: Level | null;
  rejectedOnly: boolean;
  /** id находок, которые начинаются в этом отрезке: по ним меряется позиция выноски. */
  startsFindings: string[];
  search: boolean;
  searchActive: boolean;
  pending: boolean;
  flash: boolean;
}

export function buildSegments(text: string, ranges: MarkRange[]): Segment[] {
  const len = text.length;
  const clean = ranges
    .map((r) => ({ ...r, start: Math.max(0, Math.min(len, r.start)), end: Math.max(0, Math.min(len, r.end)) }))
    .filter((r) => r.end > r.start);
  const cuts = new Set<number>([0, len]);
  for (const r of clean) {
    cuts.add(r.start);
    cuts.add(r.end);
  }
  const points = [...cuts].sort((a, b) => a - b);
  const segments: Segment[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const start = points[i];
    const end = points[i + 1];
    if (end <= start) continue;
    const covering = clean.filter((r) => r.start <= start && r.end >= end);
    const findings = covering.filter((r) => r.kind === 'finding' && r.findingId);
    const active = findings.filter((r) => !r.rejected);
    const level: Level | null = active.some((r) => r.level === 'critical') ? 'critical' : active.length ? 'warning' : null;
    segments.push({
      start,
      end,
      text: text.slice(start, end),
      findingIds: findings.map((r) => r.findingId!),
      level,
      rejectedOnly: findings.length > 0 && active.length === 0,
      startsFindings: findings.filter((r) => r.start === start).map((r) => r.findingId!),
      search: covering.some((r) => r.kind === 'search'),
      searchActive: covering.some((r) => r.kind === 'search-active'),
      pending: covering.some((r) => r.kind === 'pending'),
      flash: covering.some((r) => r.kind === 'flash'),
    });
  }
  if (!segments.length) {
    segments.push({ start: 0, end: 0, text: '', findingIds: [], level: null, rejectedOnly: false, startsFindings: [], search: false, searchActive: false, pending: false, flash: false });
  }
  return segments;
}

const fold = (s: string) => s.toLowerCase().replaceAll('ё', 'е');

/** Совпадения подстроки без учёта регистра и «ё», как `GET /documents/{id}/search`. */
export function findMatches(text: string, query: string): { start: number; end: number }[] {
  const q = fold(query.trim());
  if (q.length < 2) return [];
  const hay = fold(text);
  const out = [];
  for (let i = hay.indexOf(q); i >= 0; i = hay.indexOf(q, i + q.length)) out.push({ start: i, end: i + q.length });
  return out;
}
