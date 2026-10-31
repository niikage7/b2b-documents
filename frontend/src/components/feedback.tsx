import { AlertTriangle, Inbox, Lock, RotateCw, X } from 'lucide-react';
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { ApiError } from '../api/http';
import { Button, Skeleton } from './ui';

// --- Тосты ---------------------------------------------------------------------------------

interface Toast {
  id: number;
  text: string;
  tone?: 'error';
  action?: { label: string; onClick: () => void };
}

interface ToastApi {
  show: (text: string, opts?: { action?: Toast['action']; tone?: 'error'; duration?: number }) => void;
  error: (error: unknown) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return 'Что-то пошло не так. Повторите действие';
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const api = useMemo<ToastApi>(() => {
    const show: ToastApi['show'] = (text, opts = {}) => {
      const id = ++seq.current;
      setToasts((t) => [...t.slice(-2), { id, text, action: opts.action, tone: opts.tone }]);
      setTimeout(() => dismiss(id), opts.duration ?? (opts.action ? 6000 : 3500));
    };
    return { show, error: (e) => show(errorMessage(e), { tone: 'error', duration: 6000 }) };
  }, [dismiss]);
  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div className="toast-region" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={t.tone === 'error' ? 'toast toast--error' : 'toast'}>
              <span className="toast__text">{t.text}</span>
              {t.action && (
                <button
                  type="button"
                  className="toast__action"
                  onClick={() => {
                    t.action!.onClick();
                    dismiss(t.id);
                  }}
                >
                  {t.action.label}
                </button>
              )}
              <button type="button" className="icon-btn toast__close" aria-label="Закрыть" onClick={() => dismiss(t.id)}>
                <X size={16} />
              </button>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast вне ToastProvider');
  return ctx;
}

// --- Диалог --------------------------------------------------------------------------------

export function Dialog({
  title,
  children,
  onClose,
  actions,
  width,
}: {
  title: string;
  children?: ReactNode;
  onClose: () => void;
  actions: ReactNode;
  width?: number;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return createPortal(
    <div className="dialog-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal aria-labelledby="dialog-title" style={width ? { width } : undefined}>
        <h2 id="dialog-title" className="dialog__title">
          {title}
        </h2>
        {children && <div className="dialog__body">{children}</div>}
        <div className="dialog__actions">{actions}</div>
      </div>
    </div>,
    document.body,
  );
}

export function ConfirmDialog({
  title,
  text,
  confirmLabel,
  danger,
  loading,
  onConfirm,
  onClose,
}: {
  title: string;
  text: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      title={title}
      onClose={onClose}
      actions={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" loading={loading} onClick={onConfirm} style={danger ? { background: 'var(--critical)' } : undefined}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {text}
    </Dialog>
  );
}

// --- Меню ---------------------------------------------------------------------------------

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/** Выпадающее меню у кнопки: позиционируется по кнопке, закрывается кликом мимо и Escape. */
export function Menu({ trigger, items, align = 'right' }: { trigger: (props: { onClick: (e: React.MouseEvent) => void; 'aria-expanded': boolean }) => ReactNode; items: (MenuItem | 'separator')[]; align?: 'left' | 'right' }) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useLayoutEffect(() => {
    if (!anchor || !menuRef.current) return;
    const rect = menuRef.current.getBoundingClientRect();
    let top = anchor.bottom + 6;
    if (top + rect.height > window.innerHeight - 8) top = anchor.top - rect.height - 6;
    const left = align === 'right' ? anchor.right - rect.width : anchor.left;
    setPos({ top, left: Math.max(8, left) });
    menuRef.current.querySelector<HTMLButtonElement>('.menu__item:not(:disabled)')?.focus();
  }, [anchor, align]);

  useEffect(() => {
    if (!anchor) return;
    const close = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setAnchor(null);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setAnchor(null);
    const onScroll = () => setAnchor(null);
    document.addEventListener('mousedown', close);
    window.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('mousedown', close);
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [anchor]);

  return (
    <>
      {trigger({
        onClick: (e) => {
          e.stopPropagation();
          setPos(null);
          setAnchor(anchor ? null : (e.currentTarget as HTMLElement).getBoundingClientRect());
        },
        'aria-expanded': Boolean(anchor),
      })}
      {anchor &&
        createPortal(
          <div
            ref={menuRef}
            className="menu"
            role="menu"
            style={pos ? { top: pos.top, left: pos.left } : { top: anchor.bottom, left: anchor.left, visibility: 'hidden' }}
            onClick={(e) => e.stopPropagation()}
          >
            {items.map((item, i) =>
              item === 'separator' ? (
                <div key={i} className="menu__separator" />
              ) : (
                <button
                  key={i}
                  type="button"
                  role="menuitem"
                  className={item.danger ? 'menu__item menu__item--danger' : 'menu__item'}
                  disabled={item.disabled}
                  onClick={() => {
                    setAnchor(null);
                    item.onClick();
                  }}
                >
                  {item.icon}
                  {item.label}
                </button>
              ),
            )}
          </div>,
          document.body,
        )}
    </>
  );
}

// --- Состояния страниц ------------------------------------------------------------------

export function EmptyState({ icon, title, text, actions }: { icon?: ReactNode; title: string; text?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="state">
      <div className="state__icon">{icon ?? <Inbox size={28} />}</div>
      <div className="state__title">{title}</div>
      {text && <div className="state__text">{text}</div>}
      {actions && <div className="state__actions">{actions}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title }: { error: unknown; onRetry?: () => void; title?: string }) {
  if (error instanceof ApiError && error.status === 403) return <ForbiddenState />;
  const notFound = error instanceof ApiError && error.status === 404;
  return (
    <div className="state">
      <div className="state__icon state__icon--error">
        <AlertTriangle size={28} />
      </div>
      <div className="state__title">{title ?? (notFound ? 'Не найдено' : 'Не удалось загрузить данные')}</div>
      <div className="state__text">{notFound ? 'Возможно, объект удалили или ссылка устарела.' : errorMessage(error)}</div>
      {onRetry && !notFound && (
        <div className="state__actions">
          <Button onClick={onRetry}>
            <RotateCw size={16} />
            Повторить
          </Button>
        </div>
      )}
    </div>
  );
}

export function ForbiddenState({ text }: { text?: string }) {
  return (
    <div className="state">
      <div className="state__icon state__icon--muted">
        <Lock size={28} />
      </div>
      <div className="state__title">Нет доступа</div>
      <div className="state__text">{text ?? 'У вашей роли нет прав на этот раздел. Если доступ нужен, обратитесь к администратору.'}</div>
    </div>
  );
}

export function ListSkeleton({ rows = 5, height = 56 }: { rows?: number; height?: number }) {
  return (
    <div className="stack" style={{ gap: 12, padding: 16 }} aria-label="Загрузка" role="status">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} height={height} />
      ))}
    </div>
  );
}
