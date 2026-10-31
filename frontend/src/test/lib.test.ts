import { seedState } from '../mocks/state';
import { findingOriginal, findingsPayload, PAGE_VIEW_KINDS } from '../mocks/views';
import { daysUntil, deadlineText } from '../pages/BatchPage';
import { layoutCallouts } from '../lib/calloutLayout';
import { countLabel, formatEta, formatWhen } from '../lib/format';
import { validateRule } from '../lib/ruleValidation';
import { buildSegments, findMatches } from '../lib/segments';
import { selectionToBlock } from '../lib/selection';
import { sectionLabel } from '../pages/document/FindingCard';
import { safeNext } from '../pages/LoginPage';
import { QueryClient } from '@tanstack/react-query';
import { applyBatchEvent, keys } from '../api/queries';
import type { BatchView, DocumentCard, DocumentProgressEvent } from '../api/types';
import { refLink, refPlace } from '../pages/BatchAssist';
import { findingLink, sortDocuments } from '../pages/BatchPage';
import { isBatchRunning, lowQualityNote } from '../pages/ProcessingPage';

describe('applyBatchEvent', () => {
  it('document.progress двигает общий процент пакета без batch.progress', () => {
    const qc = new QueryClient();
    const view = {
      id: 'b',
      progress: { done: 0, total: 2, errors: 0, percent: 1 },
      documents: [
        { id: 'a', progress: 5 },
        { id: 'c', progress: 5 },
      ],
    } as unknown as BatchView;
    qc.setQueryData(keys.batch('b'), view);
    const data = { document_id: 'a', status: 'processing', stage: 'analysis', stage_label: 'Анализ по правилам', stage_detail: null, next_stage_label: null, progress: 65 } satisfies DocumentProgressEvent;
    applyBatchEvent(qc, 'b', { type: 'document.progress', data });
    expect(qc.getQueryData<BatchView>(keys.batch('b'))?.progress?.percent).toBe(35);
  });
});

describe('buildSegments', () => {
  const text = 'Поставка Товара осуществляется в течение 3 (трёх) календарных дней.';

  it('покрывает весь текст без пересечений', () => {
    const segs = buildSegments(text, [
      { kind: 'finding', findingId: 'a', level: 'warning', start: 0, end: 30 },
      { kind: 'finding', findingId: 'b', level: 'critical', start: 16, end: 50 },
    ]);
    expect(segs.map((s) => s.text).join('')).toBe(text);
    expect(segs[0]).toMatchObject({ start: 0, end: 16, level: 'warning', startsFindings: ['a'] });
    expect(segs[1]).toMatchObject({ start: 16, end: 30, level: 'critical', findingIds: ['a', 'b'], startsFindings: ['b'] });
    expect(segs[2]).toMatchObject({ start: 30, end: 50, level: 'critical' });
    expect(segs[3]).toMatchObject({ level: null, findingIds: [] });
  });

  it('отклонённая находка не даёт цвета', () => {
    const [seg] = buildSegments('abc', [{ kind: 'finding', findingId: 'a', level: 'critical', rejected: true, start: 0, end: 3 }]);
    expect(seg).toMatchObject({ level: null, rejectedOnly: true });
  });

  it('обрезает диапазоны по длине текста', () => {
    const segs = buildSegments('abc', [{ kind: 'search', start: 2, end: 10 }]);
    expect(segs.map((s) => [s.text, s.search])).toEqual([
      ['ab', false],
      ['c', true],
    ]);
  });
});

describe('findMatches', () => {
  it('ищет без учёта регистра и ё', () => {
    expect(findMatches('Трёх дней, ТРЕХ недель', 'трех')).toEqual([
      { start: 0, end: 4 },
      { start: 11, end: 15 },
    ]);
    expect(findMatches('abc', 'a')).toEqual([]);
  });
});

describe('selectionToBlock', () => {
  it('переводит выделение в смещения текста блока', () => {
    document.body.innerHTML = `<div id="root"><p><span>3.2.</span><span data-block-text="k12"><span>Поставка </span><mark>Товара</mark><span> осуществляется</span></span></p></div>`;
    const root = document.getElementById('root')!;
    const mark = root.querySelector('mark')!.firstChild!;
    const tail = root.querySelectorAll('[data-block-text] > span')[1].firstChild!;
    const range = document.createRange();
    range.setStart(mark, 0);
    range.setEnd(tail, 15);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    expect(selectionToBlock(sel, root)).toEqual({ ok: true, value: { blockId: 'k12', start: 9, end: 30, text: 'Товара осуществляется' } });
  });

  it('не принимает выделение через несколько пунктов', () => {
    document.body.innerHTML = `<div id="root"><span data-block-text="a">первый</span><span data-block-text="b">второй</span></div>`;
    const root = document.getElementById('root')!;
    const [a, b] = root.querySelectorAll('[data-block-text]');
    const range = document.createRange();
    range.setStart(a.firstChild!, 1);
    range.setEnd(b.firstChild!, 2);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    expect(selectionToBlock(sel, root)).toEqual({ ok: false, reason: 'multiple-blocks' });
  });
});

describe('layoutCallouts', () => {
  it('сдвигает выноски вниз и сворачивает далеко сдвинутые', () => {
    const out = layoutCallouts([
      { id: 'a', anchor: 0, height: 100 },
      { id: 'b', anchor: 20, height: 100 },
      { id: 'c', anchor: 400, height: 100 },
    ]);
    expect(out).toEqual([
      { id: 'a', top: 0, anchor: 0, compact: false },
      { id: 'b', top: 110, anchor: 20, compact: true },
      { id: 'c', top: 400, anchor: 400, compact: false },
    ]);
  });

  it('не сворачивает активную выноску', () => {
    const out = layoutCallouts([
      { id: 'a', anchor: 0, height: 100 },
      { id: 'b', anchor: 20, height: 100, active: true },
    ]);
    expect(out[1]).toMatchObject({ top: 110, compact: false });
  });
});

describe('validateRule', () => {
  const base = {
    code: 'R-04',
    title: 'Срок',
    description: 'Описание',
    level: 'critical' as const,
    kind: 'semantic_llm' as const,
    prompt_template: 'Проверь {фрагмент}: срок меньше {порог} дней',
    variables: [{ name: 'порог', type: 'number' as const, value: '10' }],
  };

  it('принимает корректное правило', () => {
    expect(validateRule(base)).toEqual({ fields: {}, warnings: [] });
  });

  it('находит неописанную переменную и отсутствие {фрагмент}', () => {
    expect(validateRule({ ...base, prompt_template: 'Срок меньше {лимит}' }).fields.prompt_template).toBe('Переменная {лимит} не описана');
    expect(validateRule({ ...base, prompt_template: 'Срок меньше {порог}' }).fields.prompt_template).toMatch(/\{фрагмент\}/);
  });

  it('проверяет значения и предупреждает о неиспользуемых', () => {
    const r = validateRule({ ...base, variables: [...base.variables, { name: 'x', type: 'number', value: 'abc' }] });
    expect(r.fields['variables[1].value']).toBe('Нужно число, например 10 или 0,5');
    expect(r.warnings).toEqual(['Переменная {x} описана, но не используется в инструкции']);
  });
});

describe('format', () => {
  it('склоняет и оценивает время', () => {
    expect(countLabel(1, ['файл', 'файла', 'файлов'])).toBe('1 файл');
    expect(countLabel(3, ['файл', 'файла', 'файлов'])).toBe('3 файла');
    expect(countLabel(11, ['файл', 'файла', 'файлов'])).toBe('11 файлов');
    expect(formatEta(38)).toBe('осталось около 40 секунд');
    expect(formatEta(150)).toBe('осталось около 3 минут');
    expect(formatWhen(null)).toBe('');
    expect(formatWhen(undefined)).toBe('');
  });
});

describe('sectionLabel', () => {
  it('форматирует номер, раздел и страницу', () => {
    expect(sectionLabel({ section_number: '3.2', section_title: 'Сроки поставки', page: 5 })).toBe('п. 3.2 «Сроки поставки», стр. 5');
  });

  it('работает без названия раздела', () => {
    expect(sectionLabel({ section_number: '3.2', section_title: null, page: 5 })).toBe('п. 3.2, стр. 5');
  });

  it('работает без номера пункта', () => {
    expect(sectionLabel({ section_number: null, section_title: 'Сроки', page: 2 })).toBe('раздел «Сроки», стр. 2');
  });

  it('работает только со страницей', () => {
    expect(sectionLabel({ section_number: null, section_title: null, page: 7 })).toBe('стр. 7');
  });

  it('отдаёт дефолт при отсутствии данных', () => {
    expect(sectionLabel({ section_number: null, section_title: null, page: null })).toBe('раздел не определён');
  });
});

describe('safeNext', () => {
  it('возвращает безопасные внутренние пути без изменений', () => {
    expect(safeNext('/batches')).toBe('/batches');
    expect(safeNext('/batches/b1')).toBe('/batches/b1');
    expect(safeNext('/documents/d1?page=2')).toBe('/documents/d1?page=2');
  });

  it('защищает от open redirect и внешних ссылок', () => {
    expect(safeNext('https://evil.com')).toBe('/batches');
    expect(safeNext('http://attacker.org/hack')).toBe('/batches');
    expect(safeNext('//evil.com')).toBe('/batches');
    expect(safeNext('/\\evil.com')).toBe('/batches');
    expect(safeNext('javascript:alert(1)')).toBe('/batches');
  });

  it('возвращает /batches при пустом или null параметре', () => {
    expect(safeNext(null)).toBe('/batches');
    expect(safeNext('')).toBe('/batches');
    expect(safeNext('batches')).toBe('/batches');
  });
});

describe('sortDocuments', () => {
  it('сортирует документы по уровню: сначала critical, затем warning, ok, ошибки в конце', () => {
    const docs = [
      { id: '1', filename: 'б_ok.docx', worst_level: 'ok' as const, status: 'done' as const },
      { id: '2', filename: 'а_critical.pdf', worst_level: 'critical' as const, status: 'done' as const },
      { id: '3', filename: 'в_warning.docx', worst_level: 'warning' as const, status: 'done' as const },
      { id: '4', filename: 'г_err.pdf', worst_level: null, status: 'error' as const },
      { id: '5', filename: 'б_critical.docx', worst_level: 'critical' as const, status: 'done' as const },
    ] as DocumentCard[];

    const sorted = sortDocuments(docs);
    expect(sorted.map((d) => d.id)).toEqual(['2', '5', '3', '1', '4']);
  });
});

describe('lowQualityNote', () => {
  it('возвращает null для пустого списка страниц', () => {
    expect(lowQualityNote([])).toBeNull();
  });

  it('склоняет страницы распознавания с низким качеством', () => {
    expect(lowQualityNote([1])).toBe('1 страница распознана с низким качеством');
    expect(lowQualityNote([1, 2])).toBe('2 страницы распознаны с низким качеством');
    expect(lowQualityNote([1, 2, 3, 4, 5])).toBe('5 страниц распознано с низким качеством');
  });
});

describe('isBatchRunning', () => {
  it('определяет статус обработки пакета', () => {
    expect(isBatchRunning(undefined)).toBe(false);
    expect(isBatchRunning({ status: 'done', documents: [] })).toBe(false);
    expect(isBatchRunning({ status: 'queued', documents: [] })).toBe(true);
    expect(isBatchRunning({ status: 'processing', documents: [] })).toBe(true);
    expect(
      isBatchRunning({
        status: 'done',
        documents: [{ status: 'processing' }] as DocumentCard[],
      }),
    ).toBe(true);
  });
});

describe('findingLink', () => {
  it('формирует ссылку на документ с параметром finding', () => {
    expect(findingLink('doc-123', 'f-456')).toBe('/documents/doc-123?finding=f-456');
  });
});

describe('ссылки на место в пакете', () => {
  const ref = { document_id: 'd', block_id: 'b', start: 3, end: 9, quote: 'срок', filename: 'ТЗ.docx', section_number: '4.1', page: 2 };

  it('открывает документ на цитате', () => {
    expect(refLink(ref)).toBe('/documents/d?block=b&start=3&end=9');
  });

  it('показывает файл, пункт и страницу, пропуская пустое', () => {
    expect(refPlace(ref)).toBe('ТЗ.docx, п. 4.1, стр. 2');
    expect(refPlace({ filename: 'ТЗ.docx', section_number: null, page: null })).toBe('ТЗ.docx');
  });
});

describe('срок подачи заявок', () => {
  const now = new Date(2026, 9, 31, 7, 30);
  it('считает календарные дни без учёта времени', () => {
    expect(daysUntil('2026-11-05T10:00', now)).toBe(5);
    expect(daysUntil('2026-10-31', now)).toBe(0);
  });
  it('пишет понятный текст, в том числе после срока', () => {
    expect(deadlineText('2026-11-05T10:00', now)).toBe('До окончания подачи заявок: 5 дн.');
    expect(deadlineText('2026-10-31', now)).toBe('Подача заявок заканчивается сегодня');
    expect(deadlineText('2026-10-30', now)).toBe('Срок подачи заявок прошёл');
  });
});

describe('сверка с оригиналом в моке', () => {
  const state = seedState();
  const kindOf = (documentId: string) => state.documents.find((d) => d.id === documentId)?.kind;

  it('рамки цитаты — доли размера страницы, картинка без сервера', () => {
    const finding = state.findings.find((f) => kindOf(f.document_id) === 'pdf' || kindOf(f.document_id) === 'scan')!;
    const original = findingOriginal(state, finding);
    expect(original.image_url.startsWith('data:image/svg+xml')).toBe(true);
    expect(original.rects.length).toBeGreaterThan(0);
    for (const r of original.rects) {
      expect(r.x0).toBeGreaterThanOrEqual(0);
      expect(r.x1).toBeLessThanOrEqual(1);
      expect(r.y0).toBeLessThan(r.y1);
    }
  });

  it('признак доступности зависит от вида документа', () => {
    const payload = findingsPayload(state, state.findings, null);
    for (const f of payload) expect(f.source_available).toBe(PAGE_VIEW_KINDS.includes(kindOf(f.document_id) ?? ''));
    expect(payload.some((f) => f.source_available)).toBe(true);
    expect(payload.some((f) => !f.source_available)).toBe(true);
  });
});
