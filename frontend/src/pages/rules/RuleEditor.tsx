import { useMutation, useQuery } from '@tanstack/react-query';
import { ChevronDown, ChevronUp, Info, Plus, Sparkles, Trash2, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/endpoints';
import { ApiError } from '../../api/http';
import { keys } from '../../api/queries';
import type { Level, Rule, RuleDraft, RuleInput, RuleKind, RuleTestResult, RuleVariable, VariableType } from '../../api/types';
import { errorMessage } from '../../components/feedback';
import { Button, Confidence, Field, LevelBadge, Segmented, Spinner } from '../../components/ui';
import { formatDate } from '../../lib/format';
import { RULE_KIND_HINTS, RULE_KIND_LABELS, VARIABLE_TYPE_LABELS } from '../../lib/labels';
import { SYSTEM_VARIABLE_NAMES, templateVariables, validateRule } from '../../lib/ruleValidation';

export function emptyRule(profileId: string): RuleInput {
  return {
    profile_id: profileId,
    code: '',
    title: '',
    description: '',
    level: 'warning',
    kind: 'semantic_llm',
    prompt_template: 'Проверь {фрагмент}: ',
    variables: [],
    enabled: true,
  };
}

export function ruleToInput(rule: Rule): RuleInput {
  return {
    profile_id: rule.profile_id,
    code: rule.code,
    title: rule.title,
    description: rule.description,
    level: rule.level,
    kind: rule.kind,
    prompt_template: rule.prompt_template,
    variables: rule.variables.map((v) => ({ ...v })),
    enabled: rule.enabled,
  };
}

export function sameRule(a: RuleInput, b: RuleInput) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Подсветка цитаты найденного фрагмента для результата теста. */
function Quote({ text, level }: { text: string; level?: Level }) {
  return <span className={`rule-test__quote rule-test__quote--${level ?? 'critical'}`}>«{text}»</span>;
}

/** Черновик правила из описания риска своими словами: заполняет форму, сохраняет методолог. */
function DraftPanel({ profileId, onDraft }: { profileId?: string; onDraft: (draft: RuleDraft) => void }) {
  const [description, setDescription] = useState('');
  const make = useMutation({
    mutationFn: () => api.draftRule({ description, profile_id: profileId }),
    onSuccess: (draft) => {
      onDraft(draft);
      setDescription('');
    },
  });
  return (
    <div className="rule-draft">
      <label htmlFor="rule-draft-text">
        <b>Описать риск своими словами</b>
      </label>
      <textarea
        id="rule-draft-text"
        className="textarea"
        rows={3}
        value={description}
        maxLength={2000}
        placeholder="Например: заказчик требует страховать товар за счёт поставщика на сумму больше цены контракта"
        onChange={(e) => setDescription(e.target.value)}
      />
      {make.isError && <div className="field__error">{errorMessage(make.error)}</div>}
      <div>
        <Button type="button" size="sm" loading={make.isPending} disabled={description.trim().length < 10} onClick={() => make.mutate()}>
          {!make.isPending && <Sparkles size={16} />}
          Заполнить форму с помощью ИИ
        </Button>
      </div>
    </div>
  );
}

function TestPanel({ draft, sample }: { draft: RuleInput; sample?: string }) {
  const recent = useQuery({ queryKey: keys.recentDocuments, queryFn: api.recentDocuments, staleTime: 30_000 });
  const [mode, setMode] = useState<'document' | 'text'>('document');
  const [documentId, setDocumentId] = useState('');
  const [text, setText] = useState('');
  const [state, setState] = useState<{ status: 'idle' } | { status: 'running'; started: number } | { status: 'done'; result: RuleTestResult } | { status: 'error'; message: string }>({ status: 'idle' });
  const [elapsed, setElapsed] = useState(0);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!documentId && recent.data?.length) setDocumentId(recent.data[0].id);
  }, [recent.data, documentId]);

  // Пример риска из черновика правила: тест можно запустить сразу
  useEffect(() => {
    if (sample) {
      setText(sample);
      setMode('text');
    }
  }, [sample]);

  useEffect(() => {
    if (state.status !== 'running') return;
    const t = setInterval(() => setElapsed(Math.round((Date.now() - state.started) / 1000)), 500);
    return () => clearInterval(t);
  }, [state]);

  useEffect(() => () => abort.current?.abort(), []);

  const run = async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setElapsed(0);
    setState({ status: 'running', started: Date.now() });
    try {
      const body = mode === 'document' ? { rule: draft, document_id: documentId } : { rule: draft, text };
      const result = await api.testRule(body, controller.signal);
      if (!controller.signal.aborted) setState({ status: 'done', result });
    } catch (e) {
      if ((e as Error).name === 'AbortError' || controller.signal.aborted) return;
      const message =
        e instanceof ApiError && (e.code === 'test_timeout' || e.status === 504) ? 'Проверка не уложилась в 60 секунд. Сократите текст или уточните инструкцию' : errorMessage(e);
      setState({ status: 'error', message });
    }
  };

  const canRun = mode === 'document' ? Boolean(documentId) : text.trim().length > 0 && text.length <= 20000;

  return (
    <section className="rule-test">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h3>Проверить на документе</h3>
        <Segmented
          small
          label="Источник текста"
          value={mode}
          onChange={setMode}
          options={[
            { value: 'document', label: 'Документ' },
            { value: 'text', label: 'Свой текст' },
          ]}
        />
      </div>
      {mode === 'document' ? (
        <select className="select" aria-label="Документ для проверки" value={documentId} onChange={(e) => setDocumentId(e.target.value)} disabled={!recent.data?.length}>
          {!recent.data?.length && <option value="">Нет обработанных документов</option>}
          {recent.data?.map((d) => (
            <option key={d.id} value={d.id}>
              {d.filename} — {d.batch.name}
            </option>
          ))}
        </select>
      ) : (
        <Field hint={`${text.length.toLocaleString('ru-RU')} из 20 000 символов`} error={text.length > 20000 ? 'Текст длиннее 20 000 символов' : null}>
          <textarea className="textarea" rows={5} aria-label="Текст для проверки" placeholder="Вставьте фрагмент договора или ТЗ" value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
      )}
      <div className="row">
        <Button onClick={() => void run()} disabled={!canRun || state.status === 'running'}>
          Проверить
        </Button>
        {state.status === 'running' && (
          <>
            <span className="muted row">
              <Spinner /> Проверяем… {elapsed} с из 60
            </span>
            <Button variant="ghost" size="sm" onClick={() => { abort.current?.abort(); setState({ status: 'idle' }); }}>
              Остановить
            </Button>
          </>
        )}
      </div>
      {state.status === 'error' && <div className="field__error">{state.message}</div>}
      {state.status === 'done' &&
        (state.result.findings.length ? (
          <div className="stack" style={{ gap: 10 }}>
            <div className="text-ok rule-test__count">
              Найдено фрагментов: {state.result.findings.length}
              <span className="subtle"> · {(state.result.duration_ms / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} с</span>
            </div>
            {state.result.findings.map((f, i) => (
              <div key={i} className="rule-test__item">
                <Quote text={f.quote} level={f.level} />
                <div className="rule-test__meta">
                  {[f.section_number && `п. ${f.section_number}`, f.page && `стр. ${f.page}`].filter(Boolean).join(', ')}
                  {(f.section_number || f.page) && ' · '}
                  <LevelBadge level={f.level} small />
                  {f.confidence_level && (
                    <>
                      <span>·</span>
                      <Confidence level={f.confidence_level} />
                    </>
                  )}
                </div>
                <div className="rule-test__explanation">{f.explanation}</div>
                {f.recommendation && (
                  <div className="rule-test__recommendation subtle">
                    <b>Что сделать:</b> {f.recommendation}
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="muted">Ничего не найдено: на этом тексте правило не сработает.</div>
        ))}
      {state.status === 'done' && !!state.result.considered?.length && (
        <details className="rule-test__considered">
          <summary>Модель рассмотрела и не отметила: {state.result.considered.length}</summary>
          <ul>
            {state.result.considered.map((c, i) => (
              <li key={i}>
                <Quote text={c.quote} />
                <div className="subtle">
                  {c.reason}
                  {c.note && `: ${c.note}`}
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

interface RuleEditorProps {
  rule: Rule | null;
  draft: RuleInput;
  onChange: (draft: RuleInput) => void;
  onSave: () => void;
  onCancel: () => void;
  onDelete: () => void;
  saving: boolean;
  serverErrors: Record<string, string>;
  warnings: string[];
}

export function RuleEditor({ rule, draft, onChange, onSave, onCancel, onDelete, saving, serverErrors, warnings }: RuleEditorProps) {
  const systemVars = useQuery({ queryKey: keys.systemVariables, queryFn: api.systemVariables, staleTime: Infinity });
  const [showVars, setShowVars] = useState(false);
  const [touched, setTouched] = useState(false);
  const [sample, setSample] = useState<string>();
  const templateRef = useRef<HTMLTextAreaElement>(null);
  const check = useMemo(() => validateRule(draft), [draft]);
  const errors = { ...(touched ? check.fields : {}), ...serverErrors };
  const used = templateVariables(draft.prompt_template);
  const set = <K extends keyof RuleInput>(key: K, value: RuleInput[K]) => onChange({ ...draft, [key]: value });
  const setVar = (i: number, patch: Partial<RuleVariable>) => set('variables', draft.variables.map((v, j) => (j === i ? { ...v, ...patch } : v)));

  const insertVariable = (name: string) => {
    const el = templateRef.current;
    const token = `{${name}}`;
    if (!el) return set('prompt_template', draft.prompt_template + token);
    const start = el.selectionStart ?? draft.prompt_template.length;
    const end = el.selectionEnd ?? start;
    set('prompt_template', draft.prompt_template.slice(0, start) + token + draft.prompt_template.slice(end));
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(start + token.length, start + token.length);
    });
  };

  const save = () => {
    setTouched(true);
    if (Object.keys(check.fields).length) {
      if (Object.keys(check.fields).some((k) => k.startsWith('variables'))) setShowVars(true);
      return;
    }
    onSave();
  };

  const allNames = [...SYSTEM_VARIABLE_NAMES, ...draft.variables.map((v) => v.name).filter(Boolean)];

  return (
    <aside className="card rule-editor" aria-label="Редактор правила">
      <div className="rule-editor__head">
        <div>
          <div className="mono subtle rule-editor__caption">
            {rule ? `${rule.code} · изменено ${formatDate(rule.updated_at)}${rule.updated_by ? ` · ${rule.updated_by.full_name}` : ''}` : 'Новое правило'}
          </div>
          <h2>{draft.title || rule?.title || 'Новое правило'}</h2>
        </div>
        <button type="button" className="icon-btn" aria-label="Закрыть редактор" onClick={onCancel}>
          <X size={18} />
        </button>
      </div>

      <form
        className="rule-editor__body"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        {!rule && (
          <DraftPanel
            profileId={draft.profile_id}
            onDraft={(d) => {
              onChange({ ...draft, code: d.code, title: d.title, description: d.description, level: d.level, kind: d.kind, prompt_template: d.prompt_template, variables: d.variables });
              setSample(d.risk_example);
              if (d.variables.length) setShowVars(true);
            }}
          />
        )}
        <div className="rule-editor__row">
          <Field label="Код" htmlFor="rule-code" error={errors.code}>
            <input id="rule-code" className={errors.code ? 'input input--error mono' : 'input mono'} value={draft.code} placeholder="R-09" onChange={(e) => set('code', e.target.value)} maxLength={20} />
          </Field>
          <Field label="Название" htmlFor="rule-title" error={errors.title}>
            <input id="rule-title" className={errors.title ? 'input input--error' : 'input'} value={draft.title} placeholder="Короткое название риска" onChange={(e) => set('title', e.target.value)} maxLength={200} />
          </Field>
        </div>

        <Field label="Описание для юриста" htmlFor="rule-description" error={errors.description} hint="Этот текст увидят в выноске рядом с найденным фрагментом.">
          <textarea
            id="rule-description"
            className={errors.description ? 'textarea textarea--error' : 'textarea'}
            rows={3}
            value={draft.description}
            placeholder="Чем условие опасно для поставщика"
            onChange={(e) => set('description', e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                save();
              }
            }}
          />
        </Field>

        <Field label="Уровень" error={errors.level}>
          <Segmented<Level>
            label="Уровень"
            value={draft.level}
            onChange={(v) => set('level', v)}
            options={[
              { value: 'critical', label: <><span className="dot dot--critical" /> Критично</> },
              { value: 'warning', label: <><span className="dot dot--warning" /> Внимание</> },
            ]}
          />
        </Field>

        <Field label="Тип проверки" htmlFor="rule-kind" error={errors.kind} hint={RULE_KIND_HINTS[draft.kind]}>
          <select id="rule-kind" className={errors.kind ? 'select select--error' : 'select'} value={draft.kind} onChange={(e) => set('kind', e.target.value as RuleKind)}>
            {(Object.keys(RULE_KIND_LABELS) as RuleKind[]).map((k) => (
              <option key={k} value={k}>
                {RULE_KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Инструкция для ИИ" htmlFor="rule-template" error={errors.prompt_template}>
          <textarea
            id="rule-template"
            ref={templateRef}
            className={errors.prompt_template ? 'textarea textarea--error' : 'textarea'}
            rows={6}
            value={draft.prompt_template}
            onChange={(e) => set('prompt_template', e.target.value)}
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                e.preventDefault();
                save();
              }
            }}
          />
        </Field>
        <button type="button" className="rule-editor__vars-line" aria-expanded={showVars} onClick={() => setShowVars((v) => !v)}>
          <span>
            Переменные:{' '}
            {used.length ? (
              used.map((n, i) => (
                <span key={n} className={allNames.includes(n) ? 'var-token' : 'var-token var-token--bad'}>
                  {`{${n}}`}
                  {i < used.length - 1 ? ', ' : ''}
                </span>
              ))
            ) : (
              <span className="subtle">нет</span>
            )}
          </span>
          {showVars ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </button>

        {showVars && (
          <div className="vars">
            <div className="vars__head">
              <span>Имя переменной</span>
              <span>Тип</span>
              <span>Значение</span>
              <span />
            </div>
            {systemVars.data?.map((v) => (
              <div key={v.name} className="vars__row vars__row--system">
                <button type="button" className="input input--sm mono vars__name-btn" title="Вставить в инструкцию" onClick={() => insertVariable(v.name)}>
                  {v.name}
                </button>
                <span className="input input--sm vars__readonly">{VARIABLE_TYPE_LABELS.auto}</span>
                <span className="input input--sm vars__readonly" title={v.description}>
                  {v.description}
                </span>
                <span />
              </div>
            ))}
            {draft.variables.map((v, i) => (
              <div key={i} className="vars__row">
                <Field error={errors[`variables[${i}].name`]}>
                  <input className={errors[`variables[${i}].name`] ? 'input input--sm input--error mono' : 'input input--sm mono'} aria-label="Имя переменной" value={v.name} placeholder="порог" onChange={(e) => setVar(i, { name: e.target.value })} onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault(); }} />
                </Field>
                <select className="select select--sm" aria-label="Тип переменной" value={v.type} onChange={(e) => setVar(i, { type: e.target.value as VariableType })}>
                  {(['number', 'text', 'list'] as VariableType[]).map((t) => (
                    <option key={t} value={t}>
                      {VARIABLE_TYPE_LABELS[t]}
                    </option>
                  ))}
                </select>
                <Field error={errors[`variables[${i}].value`] ?? errors[`variables[${i}].type`]}>
                  <input
                    className={errors[`variables[${i}].value`] ? 'input input--sm input--error' : 'input input--sm'}
                    aria-label="Значение переменной"
                    value={v.value}
                    placeholder={v.type === 'list' ? 'НДС; аванс; гарантия' : v.type === 'number' ? '10' : 'текст'}
                    onChange={(e) => setVar(i, { value: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        if (i === draft.variables.length - 1) {
                          set('variables', [...draft.variables, { name: '', type: 'number', value: '' }]);
                        }
                      }
                    }}
                  />
                </Field>
                <div className="row" style={{ gap: 2 }}>
                  <button type="button" className="icon-btn" title="Вставить в инструкцию" aria-label={`Вставить {${v.name}} в инструкцию`} disabled={!v.name} onClick={() => insertVariable(v.name)}>
                    <Plus size={15} />
                  </button>
                  <button type="button" className="icon-btn" aria-label="Удалить переменную" onClick={() => set('variables', draft.variables.filter((_, j) => j !== i))}>
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>
            ))}
            <button type="button" className="btn btn--link vars__add" onClick={() => set('variables', [...draft.variables, { name: '', type: 'number', value: '' }])}>
              <Plus size={15} /> Добавить переменную
            </button>
            <p className="field__hint">
              Тип «Авто» система заполняет сама при проверке. В инструкции переменная пишется в фигурных скобках, например {'{порог}'}. Список — значения через точку с
              запятой.
            </p>
          </div>
        )}

        {(warnings.length > 0 || (!touched && check.warnings.length > 0)) && (
          <div className="note">
            <Info size={16} />
            <div>{(warnings.length ? warnings : check.warnings).map((w) => <div key={w}>{w}</div>)}</div>
          </div>
        )}

        <TestPanel draft={draft} sample={sample} />

        <p className="note rule-editor__notice">
          <Info size={16} />
          Изменение правила действует только на новые пакеты: уже проверенные пакеты сохраняют прежние находки.
        </p>

        <div className="rule-editor__footer">
          {rule && (
            <Button variant="danger" onClick={onDelete}>
              Удалить правило
            </Button>
          )}
          <span className="spacer" />
          <Button onClick={onCancel}>Отмена</Button>
          <Button variant="primary" type="submit" loading={saving}>
            Сохранить
          </Button>
        </div>
      </form>
    </aside>
  );
}
