import { memo, type ReactNode, type RefObject } from 'react';
import type { Block } from '../../api/types';
import { buildSegments, type MarkRange } from '../../lib/segments';

interface BlockProps {
  block: Block;
  ranges: MarkRange[] | undefined;
  activeId: string | null;
  hoveredId: string | null;
  showPage: boolean;
}

function Segments({ block, ranges, activeId, hoveredId }: Omit<BlockProps, 'showPage'>) {
  const segments = buildSegments(block.text, ranges ?? []);
  return (
    <span className="doc-text" data-block-text={block.id}>
      {segments.map((s) => {
        const classes: string[] = [];
        if (s.findingIds.length) {
          classes.push('mk');
          if (s.level) classes.push(`mk--${s.level}`);
          else if (s.rejectedOnly) classes.push('mk--rejected');
          if (activeId && s.findingIds.includes(activeId)) classes.push('mk--active');
          else if (hoveredId && s.findingIds.includes(hoveredId)) classes.push('mk--hover');
        }
        if (s.search) classes.push('mk-search');
        if (s.searchActive) classes.push('mk-search--active');
        if (s.pending) classes.push('mk-pending');
        if (s.flash) classes.push('mk-flash');
        if (!classes.length) return <span key={s.start}>{s.text}</span>;
        return (
          <mark
            key={s.start}
            className={classes.join(' ')}
            data-fids={s.findingIds.length ? s.findingIds.join(' ') : undefined}
            data-fs={s.startsFindings.length ? s.startsFindings.join(' ') : undefined}
            data-search={s.searchActive ? 'active' : undefined}
          >
            {s.text}
          </mark>
        );
      })}
    </span>
  );
}

const BlockView = memo(function BlockView({ block, ranges, activeId, hoveredId, showPage }: BlockProps) {
  const page = showPage ? <span className="doc-page-label">стр. {block.page}</span> : null;
  const lowOcr = block.low_ocr_quality ? ' doc-block--low-ocr' : '';
  const body = <Segments block={block} ranges={ranges} activeId={activeId} hoveredId={hoveredId} />;
  if (block.kind === 'heading') {
    return (
      <h3 className={`doc-heading doc-heading--l${block.level ?? 1}${lowOcr}`} data-block-id={block.id} data-page={block.page}>
        {block.number && <span className="doc-heading__number">{block.number.replace(/\.$/, '')}. </span>}
        {body}
        {page}
      </h3>
    );
  }
  const align = block.align && block.align !== 'left' ? ` doc-block--${block.align}` : '';
  const bold = block.bold ? ' doc-block--bold' : '';
  if (block.number) {
    return (
      <div className={`doc-block doc-block--numbered${lowOcr}`} data-block-id={block.id} data-page={block.page}>
        <span className="doc-block__number">{block.number}</span>
        <p className={`doc-block__text${bold}`}>{body}</p>
        {page}
      </div>
    );
  }
  return (
    <div className={`doc-block${align}${bold}${lowOcr}`} data-block-id={block.id} data-page={block.page}>
      <p className="doc-block__text">{body}</p>
      {page}
    </div>
  );
});

function TableView({ cells, rangesByBlock, activeId, hoveredId, findingsByBlock, showPage }: {
  cells: Block[];
  rangesByBlock: Map<string, MarkRange[]>;
  activeId: string | null;
  hoveredId: string | null;
  findingsByBlock: Map<string, Set<string>>;
  showPage: boolean;
}) {
  const rows = new Map<number, Block[]>();
  for (const c of cells) {
    const row = rows.get(c.row ?? 0) ?? [];
    row.push(c);
    rows.set(c.row ?? 0, row);
  }
  const sorted = [...rows.entries()].sort((a, b) => a[0] - b[0]);
  const lowOcr = cells.some((c) => c.low_ocr_quality);
  return (
    <div className={lowOcr ? 'doc-table-wrap doc-block--low-ocr' : 'doc-table-wrap'} data-page={cells[0].page}>
      <table className="doc-table">
        <tbody>
          {sorted.map(([rowIndex, row]) => (
            <tr key={rowIndex}>
              {row
                .sort((a, b) => (a.col ?? 0) - (b.col ?? 0))
                .map((cell) => {
                  const ids = findingsByBlock.get(cell.id);
                  const Tag = rowIndex === 0 && cell.bold ? 'th' : 'td';
                  return (
                    <Tag key={cell.id} data-block-id={cell.id} data-page={cell.page}>
                      <Segments
                        block={cell}
                        ranges={rangesByBlock.get(cell.id)}
                        activeId={activeId && ids?.has(activeId) ? activeId : null}
                        hoveredId={hoveredId && ids?.has(hoveredId) ? hoveredId : null}
                      />
                    </Tag>
                  );
                })}
            </tr>
          ))}
        </tbody>
      </table>
      {showPage && <span className="doc-page-label">стр. {cells[0].page}</span>}
    </div>
  );
}

export interface DocumentTextProps {
  rootRef: RefObject<HTMLDivElement | null>;
  blocks: Block[];
  rangesByBlock: Map<string, MarkRange[]>;
  findingsByBlock: Map<string, Set<string>>;
  activeId: string | null;
  hoveredId: string | null;
  onMarkHover: (findingId: string | null) => void;
  onMarkClick: (findingIds: string[]) => void;
  onMouseUp: () => void;
  footer?: ReactNode;
}

/**
 * Документ как читаемый текст: заголовки с номерами, пункты, таблицы, номер страницы на полях.
 * Подсвечен ровно фрагмент цитаты, клик по подсветке активирует выноску.
 */
export function DocumentText({ rootRef, blocks, rangesByBlock, findingsByBlock, activeId, hoveredId, onMarkHover, onMarkClick, onMouseUp, footer }: DocumentTextProps) {
  const items: ReactNode[] = [];
  let lastPage = 0;
  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i];
    const showPage = block.page !== lastPage;
    lastPage = block.page;
    if (block.kind === 'table_cell' && block.table_id !== undefined) {
      const cells = [block];
      while (i + 1 < blocks.length && blocks[i + 1].kind === 'table_cell' && blocks[i + 1].table_id === block.table_id) cells.push(blocks[++i]);
      lastPage = cells[cells.length - 1].page;
      items.push(
        <TableView key={block.id} cells={cells} rangesByBlock={rangesByBlock} activeId={activeId} hoveredId={hoveredId} findingsByBlock={findingsByBlock} showPage={showPage} />,
      );
      continue;
    }
    const ids = findingsByBlock.get(block.id);
    items.push(
      <BlockView
        key={block.id}
        block={block}
        ranges={rangesByBlock.get(block.id)}
        activeId={activeId && ids?.has(activeId) ? activeId : null}
        hoveredId={hoveredId && ids?.has(hoveredId) ? hoveredId : null}
        showPage={showPage}
      />,
    );
  }

  return (
    <div
      ref={rootRef}
      className="doc-page"
      onMouseOver={(e) => {
        const mark = (e.target as HTMLElement).closest<HTMLElement>('[data-fids]');
        onMarkHover(mark?.dataset.fids?.split(' ')[0] ?? null);
      }}
      onMouseLeave={() => onMarkHover(null)}
      onClick={(e) => {
        if (window.getSelection()?.isCollapsed === false) return;
        const mark = (e.target as HTMLElement).closest<HTMLElement>('[data-fids]');
        if (mark?.dataset.fids) onMarkClick(mark.dataset.fids.split(' '));
      }}
      onMouseUp={onMouseUp}
    >
      {items}
      {footer}
    </div>
  );
}
