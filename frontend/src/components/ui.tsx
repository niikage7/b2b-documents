import { Fragment, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Link } from 'react-router';
import type { ConfidenceLevel, DocumentKind, FindingsCount, Level, WorstLevel } from '../api/types';
import { fileExtension } from '../lib/format';
import { CONFIDENCE_HINTS, CONFIDENCE_LABELS, LEVEL_LABELS, worstLevelLabel, worstLevelTone } from '../lib/labels';

type ButtonVariant = 'primary' | 'secondary' | 'dark' | 'ghost' | 'link' | 'danger';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: 'sm' | 'md' | 'lg';
  block?: boolean;
  icon?: boolean;
  loading?: boolean;
}

export function Button({ variant = 'secondary', size = 'md', block, icon, loading, className, children, disabled, ...rest }: ButtonProps) {
  const cls = ['btn', `btn--${variant}`, size !== 'md' && `btn--${size}`, block && 'btn--block', icon && 'btn--icon', className]
    .filter(Boolean)
    .join(' ');
  return (
    <button type="button" className={cls} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>
      {loading && <span className="spinner" aria-hidden />}
      {children}
    </button>
  );
}

export function Spinner({ large }: { large?: boolean }) {
  return <span className={large ? 'spinner spinner--lg' : 'spinner'} role="status" aria-label="Загрузка" />;
}

export function Skeleton({ width = '100%', height = 14, style }: { width?: number | string; height?: number | string; style?: React.CSSProperties }) {
  return <span className="skeleton" style={{ width, height, ...style }} aria-hidden />;
}

/** Цветная точка. Всегда рядом с подписью: цвет не работает в одиночку. */
export function LevelDot({ level, large }: { level: WorstLevel; large?: boolean }) {
  const tone = worstLevelTone(level);
  return <span className={`dot dot--${tone}${large ? ' dot--lg' : ''}`} aria-hidden />;
}

/** «● Критично 2» — бейдж уровня с числом. */
export function LevelBadge({ level, count, small }: { level: Level; count?: number; small?: boolean }) {
  return (
    <span className={`badge badge--${level}${small ? ' badge--sm' : ''}`}>
      <LevelDot level={level} />
      {LEVEL_LABELS[level]}
      {count !== undefined && <span className="tabular">{count}</span>}
    </span>
  );
}

/** Худший уровень документа или пакета: «Критично», «Без рисков», «Не проверен». */
export function WorstLevelBadge({ level, small }: { level: WorstLevel; small?: boolean }) {
  const tone = worstLevelTone(level);
  return (
    <span className={`badge badge--${tone === 'neutral' ? 'neutral' : tone}${small ? ' badge--sm' : ''}`}>
      <LevelDot level={level} />
      {worstLevelLabel(level)}
    </span>
  );
}

/** «Критично 2 · Внимание 3», а если находок нет — «Без рисков». */
export function FindingsBadges({ findings, worst, small }: { findings: FindingsCount; worst?: WorstLevel; small?: boolean }) {
  if (!findings.critical && !findings.warning) {
    return worst === 'ok' ? <WorstLevelBadge level="ok" small={small} /> : <span className="subtle">—</span>;
  }
  return (
    <span className="badges">
      {findings.critical > 0 && <LevelBadge level="critical" count={findings.critical} small={small} />}
      {findings.warning > 0 && <LevelBadge level="warning" count={findings.warning} small={small} />}
    </span>
  );
}

export function ProgressBar({ value, tone, large, label }: { value: number; tone?: 'ok'; large?: boolean; label?: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      className={['progress', tone && `progress--${tone}`, large && 'progress--lg'].filter(Boolean).join(' ')}
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      <div className="progress__bar" style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  small,
  label,
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (value: T) => void;
  small?: boolean;
  label: string;
}) {
  return (
    <div className={small ? 'segmented segmented--sm' : 'segmented'} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={o.value === value ? 'segmented__item segmented__item--active' : 'segmented__item'}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      className="toggle"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        onChange(!checked);
      }}
    />
  );
}

const FILE_ICON_TONES: Record<string, string> = {
  docx: 'doc',
  doc: 'doc',
  rtf: 'doc',
  pdf: 'pdf',
  scan: 'scan',
  zip: 'zip',
};

/** Иконка типа файла: «DOC», «PDF», «СКАН», «ZIP». */
export function FileTypeIcon({ filename, kind, bad, small }: { filename: string; kind?: DocumentKind; bad?: boolean; small?: boolean }) {
  const ext = kind ?? fileExtension(filename);
  const label = ext === 'scan' ? 'СКАН' : ext === 'docx' ? 'DOC' : (ext || '?').toUpperCase().slice(0, 4);
  const tone = bad ? 'bad' : (FILE_ICON_TONES[ext] ?? 'zip');
  return (
    <span className={`file-icon file-icon--${tone}${small ? ' file-icon--sm' : ''}`} aria-hidden>
      {label}
    </span>
  );
}

export interface Crumb {
  label: string;
  to?: string;
}

export function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav className="breadcrumbs" aria-label="Навигация">
      {items.map((c, i) => (
        <Fragment key={i}>
          {i > 0 && <span aria-hidden>/</span>}
          {c.to ? (
            <Link to={c.to}>{c.label}</Link>
          ) : (
            <span className="breadcrumbs__current" aria-current="page">
              {c.label}
            </span>
          )}
        </Fragment>
      ))}
    </nav>
  );
}

/** Уверенность ИИ: подпись и три деления, подсказка — при наведении. */
export function Confidence({ level }: { level: ConfidenceLevel }) {
  const on = { high: 3, medium: 2, low: 1 }[level];
  return (
    <span className="confidence tooltip" data-tip={CONFIDENCE_HINTS[level]}>
      уверенность {CONFIDENCE_LABELS[level]}
      <span className="confidence__bars" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span key={i} className={i < on ? 'confidence__bar confidence__bar--on' : 'confidence__bar'} />
        ))}
      </span>
    </span>
  );
}

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label?: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  htmlFor?: string;
}) {
  return (
    <div className="field">
      {label && (
        <label className="field__label" htmlFor={htmlFor}>
          {label}
        </label>
      )}
      {children}
      {error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}
    </div>
  );
}
