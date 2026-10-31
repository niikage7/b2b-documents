// Раскладка выносок в колонке справа: каждая — на уровне своего фрагмента, без наложений.
// Если места не хватает, выноска сдвигается ниже и сворачивается в одну строку; активная не сворачивается.

export interface CalloutInput {
  id: string;
  /** Верх фрагмента в координатах колонки. */
  anchor: number;
  /** Высота развёрнутой (обычной) выноски, если уже измерена. */
  height?: number;
  active?: boolean;
}

export interface CalloutPlacement {
  id: string;
  top: number;
  anchor: number;
  compact: boolean;
}

export const CALLOUT_GAP = 10;
export const COMPACT_HEIGHT = 42;
const DEFAULT_HEIGHT = 92;
/** Насколько выноску можно сдвинуть вниз от фрагмента, прежде чем свернуть её. */
const SHIFT_TOLERANCE = 28;

export function layoutCallouts(items: CalloutInput[]): CalloutPlacement[] {
  const sorted = [...items].sort((a, b) => a.anchor - b.anchor);
  const out: CalloutPlacement[] = [];
  let bottom = -Infinity;
  for (const item of sorted) {
    const top = Math.max(item.anchor, bottom + CALLOUT_GAP);
    const compact = !item.active && top - item.anchor > SHIFT_TOLERANCE;
    const height = compact ? COMPACT_HEIGHT : (item.height ?? DEFAULT_HEIGHT);
    out.push({ id: item.id, top, anchor: item.anchor, compact });
    bottom = top + height;
  }
  return out;
}
