// Выделение мышью → `block_id + start + end` в символах текста блока (FR-6.1).
// Текст блока лежит в элементе с атрибутом data-block-text; номер пункта рендерится вне его.

export interface BlockSelection {
  blockId: string;
  start: number;
  end: number;
  text: string;
}

export type SelectionResult = { ok: true; value: BlockSelection } | { ok: false; reason: 'empty' | 'multiple-blocks' | 'outside' };

function blockTextElement(node: Node | null, root: HTMLElement): HTMLElement | null {
  let el: Node | null = node;
  while (el && el !== root) {
    if (el instanceof HTMLElement && el.dataset.blockText !== undefined) return el;
    el = el.parentNode;
  }
  return null;
}

/** Смещение точки внутри элемента текста блока: длина текста от начала элемента до точки. */
export function offsetWithin(container: HTMLElement, node: Node, offset: number): number {
  const range = document.createRange();
  range.setStart(container, 0);
  range.setEnd(node, offset);
  return range.toString().length;
}

export function selectionToBlock(selection: Selection | null, root: HTMLElement): SelectionResult {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return { ok: false, reason: 'empty' };
  const range = selection.getRangeAt(0);
  const startEl = blockTextElement(range.startContainer, root);
  const endEl = blockTextElement(range.endContainer, root);
  if (!startEl || !endEl) return { ok: false, reason: 'outside' };
  if (startEl !== endEl) return { ok: false, reason: 'multiple-blocks' };
  const text = startEl.textContent ?? '';
  let start = offsetWithin(startEl, range.startContainer, range.startOffset);
  let end = offsetWithin(startEl, range.endContainer, range.endOffset);
  if (start > end) [start, end] = [end, start];
  // Пробелы по краям выделения в пометку не берём
  while (start < end && /\s/.test(text[start])) start++;
  while (end > start && /\s/.test(text[end - 1])) end--;
  if (end <= start) return { ok: false, reason: 'empty' };
  return { ok: true, value: { blockId: startEl.dataset.blockText!, start, end, text: text.slice(start, end) } };
}
