import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, ChevronLeft, ChevronRight, Download, Loader, RotateCw, ScanLine, Search } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router';
import { api } from '../../api/endpoints';
import { ApiError } from '../../api/http';
import { applyFindingMutation, keys, useBatchEvents, useDocument } from '../../api/queries';
import type { Block, DocumentView, Finding, FindingPatch, Level } from '../../api/types';
import { ConfirmDialog, ErrorState, useToast } from '../../components/feedback';
import { Breadcrumbs, Button, ProgressBar, Skeleton, Spinner } from '../../components/ui';
import { layoutCallouts } from '../../lib/calloutLayout';
import { countLabel } from '../../lib/format';
import { documentTypeLabel } from '../../lib/labels';
import { findMatches, type MarkRange } from '../../lib/segments';
import { selectionToBlock } from '../../lib/selection';
import { useDebounced } from '../../lib/useDebounced';
import { useExport } from '../../lib/useExport';
import { isBatchRunning, useRetryDocument } from '../ProcessingPage';
import { DocumentText } from './DocumentText';
import { type CardMode, FindingCard } from './FindingCard';
import { DocumentsPanel, NewMarkPopover } from './parts';

type LevelFilter = 'all' | Level | 'user';

const PANEL_KEY = 'b2b-docs-panel-collapsed';
const PAGE_CHUNK = 20;

function readCollapsed() {
  try {
    return localStorage.getItem(PANEL_KEY) === '1';
  } catch {
    return false;
  }
}

function isTyping(target: EventTarget | null) {
  const el = target as HTMLElement | null;
  return Boolean(el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable));
}

interface PendingMark {
  blockId: string;
  start: number;
  end: number;
  text: string;
  top: number;
  left: number;
}

interface SearchMatchRef {
  block_id: string;
  start: number;
  end: number;
  page: number;
}

export function DocumentPage() {
  const { documentId = '' } = useParams();
  const query = useDocument(documentId);
  const doc = query.data;
  const batchId = doc?.batch.id;
  const batch = useQuery({ queryKey: keys.batch(batchId ?? ''), queryFn: () => api.getBatch(batchId!), enabled: Boolean(batchId) });
  const processing = doc ? doc.status === 'queued' || doc.status === 'processing' : false;
  useBatchEvents(batchId, processing || isBatchRunning(batch.data));
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const toggle = () =>
    setCollapsed((v) => {
      try {
        localStorage.setItem(PANEL_KEY, v ? '0' : '1');
      } catch {
        // не запомнили — не страшно
      }
      return !v;
    });

  return (
    <div className={collapsed ? 'editor editor--collapsed' : 'editor'}>
      <DocumentsPanel batch={batch.data} currentId={documentId} collapsed={collapsed} onToggle={toggle} />
      <div className="editor__main">
        {query.isError ? (
          <div className="page">
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          </div>
        ) : !doc ? (
          <EditorSkeleton />
        ) : doc.status !== 'done' ? (
          <DocumentNotReady doc={doc} />
        ) : (
          <Editor key={doc.id} doc={doc} />
        )}
      </div>
    </div>
  );
}

function EditorSkeleton() {
  return (
    <div className="editor__body" style={{ padding: 28 }}>
      <div className="stack" style={{ gap: 14, width: 640 }}>
        <Skeleton width={240} />
        <Skeleton width={360} height={30} />
        <Skeleton height={640} style={{ marginTop: 16 }} />
      </div>
    </div>
  );
}

function DocumentNotReady({ doc }: { doc: DocumentView }) {
  const { retry, isPending } = useRetryDocument(doc.batch.id);
  const crumbs = [{ label: 'Пакеты', to: '/batches' }, { label: doc.batch.name, to: `/batches/${doc.batch.id}` }, { label: doc.filename }];
  return (
    <div className="page">
      <Breadcrumbs items={crumbs} />
      <h1 style={{ margin: '8px 0 24px' }}>{doc.filename}</h1>
      <div className="card not-ready">
        {doc.status === 'error' ? (
          <div className="state">
            <div className="state__icon state__icon--error">
              <AlertTriangle size={28} />
            </div>
            <div className="state__title">Документ не обработан</div>
            <div className="state__text">{doc.error?.message ?? 'Не удалось обработать файл'}. Остальные документы пакета доступны.</div>
            <div className="state__actions">
              <Button variant="primary" loading={isPending(doc.id)} onClick={() => void retry(doc.id)}>
                <RotateCw size={16} /> Повторить
              </Button>
              <Link to={`/batches/${doc.batch.id}`} className="btn btn--secondary">
                К разбору пакета
              </Link>
            </div>
          </div>
        ) : (
          <div className="state">
            <div className="state__icon">
              <Loader size={28} className="spin" />
            </div>
            <div className="state__title">Документ ещё обрабатывается</div>
            <div className="state__text">{doc.stage_label}. Страница обновится сама, когда проверка закончится.</div>
            <div style={{ width: 320 }}>
              <ProgressBar value={doc.progress} label={doc.stage_label} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function Editor({ doc }: { doc: DocumentView }) {
  const qc = useQueryClient();
  const toast = useToast();
  const exporter = useExport();
  const [sp, setSp] = useSearchParams();

  // --- Блоки, в том числе подгруженные при прокрутке длинного документа ---------------------

  const [extraBlocks, setExtraBlocks] = useState<Block[]>([]);
  const initialLoadedTo = doc.blocks_complete ? Infinity : Math.max(PAGE_CHUNK, ...doc.blocks.map((b) => b.page));
  const loadedTo = useRef(initialLoadedTo);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadChain = useRef<Promise<void>>(Promise.resolve());
  const totalPages = doc.pages ?? 0;
  const complete = loadedTo.current >= totalPages;

  const loadNext = useCallback(() => {
    loadChain.current = loadChain.current.then(async () => {
      if (loadedTo.current >= totalPages) return;
      const from = loadedTo.current + 1;
      const to = Math.min(totalPages, from + PAGE_CHUNK - 1);
      setLoadingMore(true);
      try {
        const res = await api.documentBlocks(doc.id, from, to);
        loadedTo.current = to;
        setExtraBlocks((prev) => [...prev, ...res.blocks]);
      } catch (e) {
        toast.error(e);
      } finally {
        setLoadingMore(false);
      }
    });
    return loadChain.current;
  }, [doc.id, totalPages, toast]);

  const ensurePage = useCallback(
    async (page: number) => {
      while (loadedTo.current < page && loadedTo.current < totalPages) await loadNext();
    },
    [loadNext, totalPages],
  );

  const blocks = useMemo(() => {
    const map = new Map<string, Block>();
    for (const b of [...doc.blocks, ...extraBlocks]) map.set(b.id, b);
    return [...map.values()].sort((a, b) => a.order - b.order);
  }, [doc.blocks, extraBlocks]);
  const blockById = useMemo(() => new Map(blocks.map((b) => [b.id, b])), [blocks]);

  // --- Находки и фильтры --------------------------------------------------------------------

  const [filter, setFilter] = useState<LevelFilter>('all');
  const [showRejected, setShowRejected] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(sp.get('finding'));
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const scrollTarget = useRef<{ kind: 'finding' | 'search' | 'block'; id?: string } | null>(sp.get('finding') ? { kind: 'finding', id: sp.get('finding')! } : null);

  const findings = doc.findings;
  const active = findings.filter((f) => f.status !== 'rejected');
  const counts = {
    all: active.length,
    critical: active.filter((f) => f.level === 'critical').length,
    warning: active.filter((f) => f.level === 'warning').length,
    user: active.filter((f) => f.source === 'user').length,
    rejected: findings.length - active.length,
  };

  const sortKey = useCallback(
    (f: Finding) => {
      const block = blockById.get(f.block_id);
      return block ? block.order : (f.page ?? 0) * 100_000;
    },
    [blockById],
  );

  const visible = useMemo(
    () =>
      findings
        .filter((f) => (showRejected || f.status !== 'rejected') && (filter === 'all' || (filter === 'user' ? f.source === 'user' : f.level === filter)))
        .sort((a, b) => sortKey(a) - sortKey(b) || a.start - b.start),
    [findings, showRejected, filter, sortKey],
  );
  const navigable = visible.filter((f) => f.status !== 'rejected');
  const activeIndex = navigable.findIndex((f) => f.id === activeId);

  // --- Поиск по тексту ----------------------------------------------------------------------

  const [search, setSearch] = useState('');
  const debouncedSearch = useDebounced(search.trim(), 250);
  const [matches, setMatches] = useState<SearchMatchRef[]>([]);
  const [matchIndex, setMatchIndex] = useState(0);
  // Первый Enter ведёт к первому совпадению, следующие — к следующим
  const searchStarted = useRef(false);
  useEffect(() => {
    searchStarted.current = false;
  }, [matches]);

  useEffect(() => {
    let cancelled = false;
    if (debouncedSearch.length < 2) {
      setMatches([]);
      return;
    }
    if (doc.blocks_complete) {
      const out: SearchMatchRef[] = [];
      for (const b of blocks) for (const m of findMatches(b.text, debouncedSearch)) out.push({ block_id: b.id, page: b.page, ...m });
      setMatches(out.slice(0, 200));
      setMatchIndex(0);
    } else {
      api.searchDocument(doc.id, debouncedSearch).then(
        (res) => {
          if (cancelled) return;
          setMatches(res.matches);
          setMatchIndex(0);
        },
        () => !cancelled && setMatches([]),
      );
    }
    return () => {
      cancelled = true;
    };
  }, [debouncedSearch, doc.blocks_complete, doc.id, blocks]);

  const goToMatch = async (index: number) => {
    if (!matches.length) return;
    const i = (index + matches.length) % matches.length;
    setMatchIndex(i);
    await ensurePage(matches[i].page);
    scrollTarget.current = { kind: 'search' };
  };

  // --- Ручная пометка и временная подсветка --------------------------------------------------

  const [pending, setPending] = useState<PendingMark | null>(null);
  const [flash, setFlash] = useState<{ blockId: string; start: number; end: number } | null>(() => {
    const block = sp.get('block');
    return block ? { blockId: block, start: Number(sp.get('start') ?? 0), end: Number(sp.get('end') ?? 0) } : null;
  });
  useEffect(() => {
    if (!flash) return;
    scrollTarget.current = { kind: 'block', id: flash.blockId };
    const t = setTimeout(() => setFlash(null), 3500);
    return () => clearTimeout(t);
  }, [flash]);

  const rangesByBlock = useMemo(() => {
    const map = new Map<string, MarkRange[]>();
    const add = (blockId: string, r: MarkRange) => {
      const list = map.get(blockId) ?? [];
      list.push(r);
      map.set(blockId, list);
    };
    for (const f of visible) add(f.block_id, { kind: 'finding', findingId: f.id, level: f.level, rejected: f.status === 'rejected', start: f.start, end: f.end });
    matches.forEach((m, i) => add(m.block_id, { kind: i === matchIndex ? 'search-active' : 'search', start: m.start, end: m.end }));
    if (pending) add(pending.blockId, { kind: 'pending', start: pending.start, end: pending.end });
    if (flash) add(flash.blockId, { kind: 'flash', start: flash.start, end: flash.end });
    return map;
  }, [visible, matches, matchIndex, pending, flash]);

  const findingsByBlock = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const f of visible) {
      const set = map.get(f.block_id) ?? new Set();
      set.add(f.id);
      map.set(f.block_id, set);
    }
    return map;
  }, [visible]);

  // --- Раскладка выносок -------------------------------------------------------------------

  const textRef = useRef<HTMLDivElement>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  const cardEls = useRef(new Map<string, HTMLDivElement>());
  const [anchors, setAnchors] = useState<Record<string, number>>({});
  const [heights, setHeights] = useState<Record<string, number>>({});
  const [textHeight, setTextHeight] = useState(0);

  const measure = useCallback(() => {
    const root = textRef.current;
    const col = columnRef.current;
    if (!root || !col) return;
    const base = col.getBoundingClientRect().top;
    const next: Record<string, number> = {};
    for (const f of visible) {
      const el = root.querySelector(`[data-fs~="${CSS.escape(f.id)}"]`);
      if (el) next[f.id] = Math.round(el.getBoundingClientRect().top - base - 4);
    }
    setAnchors((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
    setTextHeight(root.offsetHeight);
  }, [visible]);

  useLayoutEffect(measure, [measure, rangesByBlock, blocks]);
  useEffect(() => {
    const root = textRef.current;
    if (!root) return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(root);
    void document.fonts?.ready.then(() => measure());
    return () => ro.disconnect();
  }, [measure]);

  const placements = useMemo(
    () => layoutCallouts(visible.filter((f) => anchors[f.id] !== undefined).map((f) => ({ id: f.id, anchor: anchors[f.id], height: heights[f.id], active: f.id === activeId }))),
    [visible, anchors, heights, activeId],
  );
  const placementById = useMemo(() => new Map(placements.map((p) => [p.id, p])), [placements]);

  const cardObserver = useMemo(
    () =>
      new ResizeObserver((entries) => {
        setHeights((prev) => {
          let changed = false;
          const next = { ...prev };
          for (const entry of entries) {
            const el = entry.target as HTMLElement;
            if (el.classList.contains('fc--compact')) continue;
            const id = el.dataset.findingId!;
            const h = Math.round(el.offsetHeight);
            if (next[id] !== h) {
              next[id] = h;
              changed = true;
            }
          }
          return changed ? next : prev;
        });
      }),
    [],
  );
  useEffect(() => () => cardObserver.disconnect(), [cardObserver]);

  const cardRef = (id: string) => (el: HTMLDivElement | null) => {
    const prev = cardEls.current.get(id);
    if (prev && prev !== el) cardObserver.unobserve(prev);
    if (el) {
      cardEls.current.set(id, el);
      cardObserver.observe(el);
    } else {
      cardEls.current.delete(id);
    }
  };

  // Прокрутка к цели после отрисовки: находка, совпадение поиска или блок из ссылки
  useEffect(() => {
    const target = scrollTarget.current;
    const root = textRef.current;
    if (!target || !root) return;
    let el: Element | null = null;
    if (target.kind === 'finding' && target.id) el = root.querySelector(`[data-fs~="${CSS.escape(target.id)}"]`);
    if (target.kind === 'search') el = root.querySelector('[data-search="active"]');
    if (target.kind === 'block' && target.id) el = root.querySelector(`[data-block-id="${CSS.escape(target.id)}"]`);
    if (el) {
      scrollTarget.current = null;
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  });

  const activate = useCallback(
    async (id: string | null, opts: { scroll?: boolean } = {}) => {
      setActiveId(id);
      setPending(null);
      if (sp.get('finding') || sp.get('block')) setSp({}, { replace: true });
      if (!id) return;
      const f = findings.find((x) => x.id === id);
      if (f && !blockById.has(f.block_id) && f.page) await ensurePage(f.page);
      if (opts.scroll) scrollTarget.current = { kind: 'finding', id };
    },
    [findings, blockById, ensurePage, sp, setSp],
  );

  const step = useCallback(
    (delta: number) => {
      if (!navigable.length) return;
      const base = activeIndex < 0 ? (delta > 0 ? -1 : 0) : activeIndex;
      const next = navigable[(base + delta + navigable.length) % navigable.length];
      void activate(next.id, { scroll: true });
    },
    [navigable, activeIndex, activate],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'j' || e.key === 'о') step(1);
      else if (e.key === 'k' || e.key === 'л') step(-1);
      else if (e.key === 'Escape') {
        setPending(null);
        setActiveId(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [step]);

  // Сменили фильтр и активная находка скрылась — снимаем выбор
  useEffect(() => {
    if (activeId && !visible.some((f) => f.id === activeId) && findings.some((f) => f.id === activeId)) setActiveId(null);
  }, [visible, activeId, findings]);

  // --- Изменение находок -------------------------------------------------------------------

  const replaceFinding = (finding: Finding) =>
    qc.setQueryData<DocumentView>(keys.document(doc.id), (d) => d && { ...d, findings: d.findings.map((f) => (f.id === finding.id ? finding : f)) });

  const patchFinding = async (f: Finding, patch: Omit<FindingPatch, 'version'>, opts: { undoable?: boolean } = {}) => {
    try {
      const res = await api.updateFinding(f.id, { ...patch, version: f.version });
      applyFindingMutation(qc, doc.batch.id, res);
      if (patch.status === 'rejected') {
        if (!showRejected && activeId === f.id) {
          const rest = navigable.filter((x) => x.id !== f.id);
          const after = rest.find((x) => sortKey(x) > sortKey(f) || (sortKey(x) === sortKey(f) && x.start > f.start)) ?? rest[0];
          setActiveId(after?.id ?? null);
        }
        if (opts.undoable) {
          toast.show('Находка отклонена', {
            action: {
              label: 'Отменить',
              onClick: () => {
                api.updateFinding(f.id, { status: 'new', version: res.finding.version }).then(
                  (undo) => {
                    applyFindingMutation(qc, doc.batch.id, undo);
                    setActiveId(f.id);
                  },
                  (e) => toast.error(e),
                );
              },
            },
          });
        }
      } else if (patch.status === 'accepted') toast.show('Находка принята');
      else if (patch.status === 'new') toast.show('Решение отменено');
      else if (patch.level || patch.explanation !== undefined) toast.show('Изменения сохранены');
      else if (patch.comment !== undefined) toast.show('Комментарий сохранён');
    } catch (e) {
      if (e instanceof ApiError && e.code === 'version_conflict' && e.details.current) {
        replaceFinding(e.details.current as Finding);
        toast.show('Находку уже изменил другой пользователь — показано актуальное состояние', { tone: 'error', duration: 6000 });
      } else {
        toast.error(e);
      }
      throw e;
    }
  };

  const [deleting, setDeleting] = useState<Finding | null>(null);
  const [deletingBusy, setDeletingBusy] = useState(false);
  const deleteFinding = async () => {
    if (!deleting) return;
    setDeletingBusy(true);
    try {
      await api.deleteFinding(deleting.id);
      qc.setQueryData<DocumentView>(keys.document(doc.id), (d) => d && { ...d, findings: d.findings.filter((f) => f.id !== deleting.id) });
      void qc.invalidateQueries({ queryKey: keys.document(doc.id) });
      void qc.invalidateQueries({ queryKey: keys.batch(doc.batch.id) });
      if (activeId === deleting.id) setActiveId(null);
      toast.show('Пометка удалена');
      setDeleting(null);
    } catch (e) {
      toast.error(e);
    } finally {
      setDeletingBusy(false);
    }
  };

  const onMouseUp = () => {
    const root = textRef.current;
    const col = columnRef.current;
    if (!root || !col) return;
    const sel = window.getSelection();
    const result = selectionToBlock(sel, root);
    if (!result.ok) {
      if (result.reason === 'multiple-blocks') {
        toast.show('Выделите фрагмент внутри одного пункта: пометка не может захватывать несколько пунктов');
        sel?.removeAllRanges();
      }
      return;
    }
    const rect = sel!.getRangeAt(0).getBoundingClientRect();
    const base = root.getBoundingClientRect();
    const left = Math.min(Math.max(8, rect.left - base.left), base.width - 330);
    setPending({ ...result.value, top: rect.bottom - base.top + 8, left });
    setActiveId(null);
    sel!.removeAllRanges();
  };

  const addMark = async (level: Level, comment: string) => {
    if (!pending) return;
    try {
      const res = await api.createFinding(doc.id, { block_id: pending.blockId, start: pending.start, end: pending.end, level, comment: comment || undefined });
      applyFindingMutation(qc, doc.batch.id, res);
      if (filter !== 'all' && filter !== 'user' && filter !== level) setFilter('all');
      setPending(null);
      setActiveId(res.finding.id);
      toast.show('Пометка добавлена');
    } catch (e) {
      toast.error(e);
    }
  };

  // --- Отрисовка ---------------------------------------------------------------------------

  const lastPlacement = placements[placements.length - 1];
  const columnHeight = Math.max(textHeight, lastPlacement ? lastPlacement.top + (heights[lastPlacement.id] ?? 120) + 40 : 0);
  const noRisks = doc.findings_summary.critical + doc.findings_summary.warning === 0;
  const lowPages = doc.ocr_low_quality_pages;
  const review = doc.review;
  const crumbs = [{ label: 'Пакеты', to: '/batches' }, { label: doc.batch.name, to: `/batches/${doc.batch.id}` }, { label: doc.filename }];
  const filterChips: { key: LevelFilter; label: string; count: number; tone?: Level }[] = [
    { key: 'all', label: 'Все', count: counts.all },
    { key: 'critical', label: 'Критично', count: counts.critical, tone: 'critical' },
    { key: 'warning', label: 'Внимание', count: counts.warning, tone: 'warning' },
    ...(counts.user ? [{ key: 'user' as const, label: 'Ручные', count: counts.user }] : []),
  ];

  return (
    <>
      <header className="editor__top">
        <div className="editor__top-row">
          <div className="editor__title">
            <Breadcrumbs items={crumbs} />
            <div className="row" style={{ gap: 10, alignItems: 'baseline' }}>
              <h1 className="editor__filename">{doc.filename}</h1>
              <span className="muted nowrap">
                {documentTypeLabel(doc.kind)}
                {doc.pages ? ` · ${doc.pages} стр.` : ''}
                {review.total > 0 && ` · проверено ${review.checked} из ${review.total}`}
              </span>
            </div>
          </div>
          <Button variant="primary" loading={exporter.isPending(`document:${doc.id}`)} onClick={() => void exporter.downloadDocument(doc.id)}>
            {!exporter.isPending(`document:${doc.id}`) && <Download size={17} />}
            Скачать документ
          </Button>
        </div>
        <div className="editor__toolbar">
          <div className="row" role="group" aria-label="Фильтр находок">
            {filterChips.map((c) => (
              <button
                key={c.key}
                type="button"
                className={['chip', c.tone && `chip--${c.tone}`, filter === c.key && 'chip--active'].filter(Boolean).join(' ')}
                aria-pressed={filter === c.key}
                onClick={() => setFilter(c.key)}
              >
                {c.tone && <span className={`dot dot--${c.tone}`} />}
                {c.label} <span className="chip__count">{c.count}</span>
              </button>
            ))}
            <label className="checkbox editor__rejected">
              <input type="checkbox" checked={showRejected} onChange={(e) => setShowRejected(e.target.checked)} />
              Показать отклонённые{counts.rejected ? ` (${counts.rejected})` : ''}
            </label>
          </div>
          <div className="spacer" />
          <div className="row editor__nav">
            <Button icon size="sm" onClick={() => step(-1)} disabled={!navigable.length} aria-label="Предыдущая находка (K)" title="Предыдущая находка · K">
              <ChevronLeft size={18} />
            </Button>
            <span className="tabular nowrap editor__nav-count">{navigable.length ? `${activeIndex >= 0 ? activeIndex + 1 : '–'} из ${navigable.length}` : 'нет находок'}</span>
            <Button icon size="sm" onClick={() => step(1)} disabled={!navigable.length} aria-label="Следующая находка (J)" title="Следующая находка · J">
              <ChevronRight size={18} />
            </Button>
          </div>
          <div className="input-icon editor__search">
            <Search size={16} />
            <input
              className="input input--sm"
              type="search"
              placeholder="Поиск по тексту"
              aria-label="Поиск по тексту документа"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  const delta = !searchStarted.current ? 0 : e.shiftKey ? -1 : 1;
                  searchStarted.current = true;
                  void goToMatch(matchIndex + delta);
                }
              }}
            />
            {debouncedSearch.length >= 2 && (
              <span className="input-icon__end editor__search-count tabular">{matches.length ? `${matchIndex + 1}/${matches.length}` : '0'}</span>
            )}
          </div>
        </div>
      </header>

      <div className="editor__body">
        <div className="editor__banners">
          {noRisks && (
            <div className="banner banner--ok">
              <CheckCircle2 size={18} /> Рисков не найдено. Если заметили риск сами, выделите фрагмент текста — появится меню ручной пометки.
            </div>
          )}
          {doc.kind === 'scan' && (
            <div className={lowPages.length ? 'banner banner--warning' : 'banner banner--info'}>
              <ScanLine size={18} />
              <span>
                Текст распознан со скана
                {doc.ocr_confidence ? ` (точность около ${Math.round(doc.ocr_confidence * 100)} %)` : ''}.
                {lowPages.length > 0 &&
                  ` ${lowPages.length === 1 ? 'Страница' : 'Страницы'} ${lowPages.join(', ')} ${lowPages.length === 1 ? 'распознана' : 'распознаны'} с низким качеством — сверьте их с оригиналом.`}
              </span>
            </div>
          )}
        </div>

        <div className="editor__grid">
          <div className="editor__text-col">
            <DocumentText
              rootRef={textRef}
              blocks={blocks}
              rangesByBlock={rangesByBlock}
              findingsByBlock={findingsByBlock}
              activeId={activeId}
              hoveredId={hoveredId}
              onMarkHover={setHoveredId}
              onMarkClick={(ids) => {
                const next = ids.includes(activeId ?? '') && ids.length > 1 ? ids[(ids.indexOf(activeId!) + 1) % ids.length] : ids[0];
                void activate(next);
                requestAnimationFrame(() => cardEls.current.get(next)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
              }}
              onMouseUp={onMouseUp}
              footer={
                <>
                  {!complete && <LoadMore onVisible={() => void loadNext()} loading={loadingMore} loadedTo={loadedTo.current} total={totalPages} />}
                  {pending && (
                    <NewMarkPopover top={pending.top} left={pending.left} quote={pending.text} onAdd={addMark} onCancel={() => setPending(null)} />
                  )}
                </>
              }
            />
          </div>

          <svg className="editor__links" height={columnHeight} aria-hidden>
            {placements.map((p) => {
              const f = visible.find((x) => x.id === p.id);
              if (!f) return null;
              const on = p.id === activeId || p.id === hoveredId;
              return (
                <path
                  key={p.id}
                  d={`M0 ${p.anchor + 12} C 18 ${p.anchor + 12}, 18 ${p.top + 18}, 36 ${p.top + 18}`}
                  className={on ? `editor__link editor__link--${f.level} editor__link--on` : 'editor__link'}
                />
              );
            })}
          </svg>

          <div ref={columnRef} className="callouts" style={{ height: columnHeight }} aria-label="Выноски с находками">
            {visible.map((f) => {
              const p = placementById.get(f.id);
              if (!p) return null;
              const mode: CardMode = f.id === activeId ? 'expanded' : p.compact ? 'compact' : 'summary';
              const index = navigable.findIndex((x) => x.id === f.id);
              return (
                <FindingCard
                  key={f.id}
                  ref={cardRef(f.id)}
                  finding={f}
                  mode={mode}
                  hovered={hoveredId === f.id}
                  position={index >= 0 ? { index: index + 1, total: navigable.length } : null}
                  style={{ top: p.top }}
                  onActivate={() => void activate(f.id, { scroll: true })}
                  onClose={() => setActiveId(null)}
                  onHover={(h) => setHoveredId(h ? f.id : null)}
                  onSection={() => {
                    scrollTarget.current = { kind: 'block', id: f.section_block_id ?? f.block_id };
                    setFlash({ blockId: f.block_id, start: f.start, end: f.end });
                  }}
                  onPatch={(patch, opts) => patchFinding(f, patch, opts)}
                  onDelete={() => setDeleting(f)}
                />
              );
            })}
          </div>
        </div>
      </div>

      <MiniMap placements={placements} visible={visible} height={columnHeight} activeId={activeId} onPick={(id) => void activate(id, { scroll: true })} />

      {deleting && (
        <ConfirmDialog
          title="Удалить пометку?"
          text={<>Ручная пометка «{deleting.quote.length > 80 ? `${deleting.quote.slice(0, 80)}…` : deleting.quote}» будет удалена без возможности восстановления.</>}
          confirmLabel="Удалить"
          danger
          loading={deletingBusy}
          onConfirm={() => void deleteFinding()}
          onClose={() => setDeleting(null)}
        />
      )}
    </>
  );
}

function LoadMore({ onVisible, loading, loadedTo, total }: { onVisible: () => void; loading: boolean; loadedTo: number; total: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => entries.some((e) => e.isIntersecting) && onVisible(), { rootMargin: '600px' });
    io.observe(el);
    return () => io.disconnect();
  }, [onVisible]);
  return (
    <div ref={ref} className="doc-load-more">
      {loading ? <Spinner /> : null}
      <span className="muted">
        Показаны страницы 1–{loadedTo} из {total}. {loading ? 'Загружаем следующие…' : 'Прокрутите ниже, чтобы загрузить остальные.'}
      </span>
    </div>
  );
}

/** Мини-карта находок на полосе прокрутки: цветные засечки, клик — к находке. */
function MiniMap({ placements, visible, height, activeId, onPick }: { placements: { id: string; anchor: number }[]; visible: Finding[]; height: number; activeId: string | null; onPick: (id: string) => void }) {
  if (!placements.length || height < 1200) return null;
  const byId = new Map(visible.map((f) => [f.id, f]));
  return (
    <div className="minimap" aria-label={`Карта находок: ${countLabel(placements.length, ['находка', 'находки', 'находок'])}`}>
      {placements.map((p) => {
        const f = byId.get(p.id);
        if (!f) return null;
        return (
          <button
            key={p.id}
            type="button"
            className={`minimap__tick minimap__tick--${f.status === 'rejected' ? 'rejected' : f.level}${p.id === activeId ? ' minimap__tick--active' : ''}`}
            style={{ top: `${(p.anchor / height) * 100}%` }}
            title={`${f.rule?.code ?? 'Ручная'} · ${f.rule?.title ?? f.quote.slice(0, 40)}`}
            onClick={() => onPick(p.id)}
          />
        );
      })}
    </div>
  );
}
