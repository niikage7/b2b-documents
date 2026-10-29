import { layoutCallouts } from '../lib/calloutLayout';
import { countLabel, formatEta, formatWhen } from '../lib/format';
import { validateRule } from '../lib/ruleValidation';
import { buildSegments, findMatches } from '../lib/segments';
import { selectionToBlock } from '../lib/selection';
import { sectionLabel } from '../pages/document/FindingCard';
import { safeNext } from '../pages/LoginPage';
import type { DocumentCard } from '../api/types';
import { findingLink, sortDocuments } from '../pages/BatchPage';
import { isBatchRunning, lowQualityNote } from '../pages/ProcessingPage';

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
