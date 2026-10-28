import { Eye, EyeOff, Info } from 'lucide-react';
import { type FormEvent, useState } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router';
import { api } from '../api/endpoints';
import { ApiError, MOCK_API } from '../api/http';
import { useAuth } from '../auth';
import { Logo } from '../components/AppLayout';
import { errorMessage } from '../components/feedback';
import { Button, Field } from '../components/ui';
import { countLabel } from '../lib/format';

function safeNext(next: string | null): string {
  // Возврат только на свои страницы: не уводим на внешний адрес из параметра
  return next && next.startsWith('/') && !next.startsWith('//') ? next : '/batches';
}

export function LoginPage() {
  const { me, signIn } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expired = params.get('expired') === '1';
  const next = safeNext(params.get('next'));

  if (me) return <Navigate to={next} replace />;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!login.trim() || !password) return;
    setSubmitting(true);
    setError(null);
    try {
      const user = await api.login(login.trim(), password);
      signIn(user);
      navigate(next, { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.code === 'too_many_attempts') {
        const seconds = Number(err.details.retry_after_seconds ?? 300);
        setError(`Слишком много попыток входа. Попробуйте через ${countLabel(Math.max(1, Math.ceil(seconds / 60)), ['минуту', 'минуты', 'минут'])}`);
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login">
      <section className="login__hero">
        <Logo />
        <div className="login__pitch">
          <h1>Проверка тендерной документации на риски</h1>
          <p>Загрузите пакет документов — система найдёт рискованные условия, процитирует их и укажет раздел документа.</p>
        </div>
        <ul className="login__legend" aria-label="Уровни риска">
          <li>
            <span className="dot dot--critical" /> Критично
          </li>
          <li>
            <span className="dot dot--warning" /> Внимание
          </li>
          <li>
            <span className="dot dot--ok" /> Без рисков
          </li>
        </ul>
      </section>
      <section className="login__panel">
        <form className="login__form" onSubmit={onSubmit} noValidate>
          <h1>Вход</h1>
          <p className="muted">Используйте учётную запись, выданную администратором.</p>
          {expired && !error && (
            <div className="banner banner--info" role="status">
              <Info size={18} /> Сессия завершена, войдите снова
            </div>
          )}
          <Field label="Логин или email" htmlFor="login">
            <input
              id="login"
              className="input"
              autoComplete="username"
              autoFocus
              value={login}
              onChange={(e) => setLogin(e.target.value)}
              placeholder="m.volkova@company.ru"
            />
          </Field>
          <Field label="Пароль" htmlFor="password">
            <div className="input-icon">
              <input
                id="password"
                className="input"
                style={{ paddingLeft: 12, paddingRight: 44 }}
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <span className="input-icon__end">
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </span>
            </div>
          </Field>
          {error && (
            <div className="field__error login__error" role="alert">
              {error}
            </div>
          )}
          <div>
            <Button type="submit" variant="primary" size="lg" loading={submitting} disabled={!login.trim() || !password}>
              Войти
            </Button>
          </div>
          <p className="muted">Нет доступа? Обратитесь к администратору.</p>
          {MOCK_API && (
            <div className="note login__demo">
              <Info size={16} />
              <div>
                Демо-режим без бэкенда. Логины: <b>methodologist</b>, <b>lawyer</b>, <b>specialist</b>; пароль — <b>demo</b>.{' '}
                <button type="button" className="btn btn--link" onClick={() => (window as unknown as { __resetMockApi?: () => void }).__resetMockApi?.()}>
                  Сбросить демо-данные
                </button>
              </div>
            </div>
          )}
        </form>
      </section>
    </div>
  );
}
