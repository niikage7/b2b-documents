import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ListChecks, MoreHorizontal, Pencil, Plus, Search, SearchX, Star, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useBlocker } from 'react-router';
import { api } from '../../api/endpoints';
import { ApiError } from '../../api/http';
import { keys, useProfiles } from '../../api/queries';
import type { Level, Rule, RuleInput, RuleKind, RuleListItem, RuleProfile } from '../../api/types';
import { useAuth } from '../../auth';
import { ConfirmDialog, Dialog, EmptyState, ErrorState, errorMessage, ForbiddenState, ListSkeleton, Menu, useToast } from '../../components/feedback';
import { Button, Field, LevelBadge, Skeleton, Toggle } from '../../components/ui';
import { RULE_KIND_LABELS } from '../../lib/labels';
import { useDebounced } from '../../lib/useDebounced';
import { emptyRule, RuleEditor, ruleToInput, sameRule } from './RuleEditor';

type Selection = { kind: 'none' } | { kind: 'new' } | { kind: 'rule'; id: string };

function ProfileDialog({ profile, profiles, onClose, onSaved }: { profile: RuleProfile | null; profiles: RuleProfile[]; onClose: () => void; onSaved: (p: RuleProfile) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(profile?.name ?? '');
  const [shortName, setShortName] = useState(profile?.short_name ?? '');
  const [copyFrom, setCopyFrom] = useState(profiles.find((p) => p.is_default)?.id ?? '');
  const mutation = useMutation({
    mutationFn: () =>
      profile
        ? api.updateProfile(profile.id, { name: name.trim(), short_name: shortName.trim() || name.trim() })
        : api.createProfile({ name: name.trim(), short_name: shortName.trim() || name.trim(), copy_from_profile_id: copyFrom || undefined }),
    onSuccess: (p) => {
      void qc.invalidateQueries({ queryKey: keys.profiles });
      onSaved(p);
    },
  });
  return (
    <Dialog
      title={profile ? 'Переименовать профиль' : 'Новый профиль правил'}
      onClose={onClose}
      actions={
        <>
          <Button onClick={onClose}>Отмена</Button>
          <Button variant="primary" disabled={!name.trim()} loading={mutation.isPending} onClick={() => mutation.mutate()}>
            {profile ? 'Сохранить' : 'Создать профиль'}
          </Button>
        </>
      }
    >
      <form className="stack" style={{ gap: 14 }} onSubmit={(e) => { e.preventDefault(); if (name.trim()) mutation.mutate(); }}>
        <Field label="Название" htmlFor="profile-name">
          <input id="profile-name" className="input" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="44-ФЗ, поставка товаров" />
        </Field>
        <Field label="Короткое название" htmlFor="profile-short" hint="Показывается в списке пакетов. Если не заполнить — возьмём полное название">
          <input id="profile-short" className="input" value={shortName} onChange={(e) => setShortName(e.target.value)} placeholder="44-ФЗ, поставка" />
        </Field>
        {!profile && (
          <Field label="Скопировать правила из профиля" htmlFor="profile-copy">
            <select id="profile-copy" className="select" value={copyFrom} onChange={(e) => setCopyFrom(e.target.value)}>
              <option value="">Не копировать — пустой профиль</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
        )}
        {mutation.error && <div className="field__error">{errorMessage(mutation.error)}</div>}
      </form>
    </Dialog>
  );
}

export function RulesPage() {
  const { canEditRules } = useAuth();
  if (!canEditRules) {
    return (
      <div className="page">
        <h1>Правила</h1>
        <ForbiddenState text="Чек-лист правил ведёт методолог. Если нужно изменить правило, обратитесь к нему или к администратору." />
      </div>
    );
  }
  return <RulesWorkspace />;
}

function RulesWorkspace() {
  const qc = useQueryClient();
  const toast = useToast();
  const profiles = useProfiles();
  const [profileId, setProfileId] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const search = useDebounced(searchInput.trim(), 250);
  const [kind, setKind] = useState<RuleKind | ''>('');
  const [level, setLevel] = useState<Level | ''>('');
  const [selection, setSelection] = useState<Selection>({ kind: 'none' });
  const [draft, setDraft] = useState<RuleInput | null>(null);
  const [original, setOriginal] = useState<RuleInput | null>(null);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [warnings, setWarnings] = useState<string[]>([]);
  const [confirm, setConfirm] = useState<{ title: string; text: string; label: string; danger?: boolean; action: () => void } | null>(null);
  const [profileDialog, setProfileDialog] = useState<'new' | RuleProfile | null>(null);

  useEffect(() => {
    if (!profileId && profiles.data?.length) setProfileId((profiles.data.find((p) => p.is_default) ?? profiles.data[0]).id);
  }, [profiles.data, profileId]);

  const rules = useQuery({
    queryKey: [...keys.rules(profileId), { search, kind }],
    queryFn: () => api.listRules({ profile_id: profileId, search: search || undefined, kind: kind || undefined }),
    enabled: Boolean(profileId),
  });
  const ruleId = selection.kind === 'rule' ? selection.id : '';
  const rule = useQuery({ queryKey: keys.rule(ruleId), queryFn: () => api.getRule(ruleId), enabled: Boolean(ruleId) });

  // Открыли правило — черновик из сохранённой версии
  useEffect(() => {
    if (selection.kind === 'rule' && rule.data && rule.data.id === selection.id) {
      const input = ruleToInput(rule.data);
      setDraft(input);
      setOriginal(input);
      setServerErrors({});
      setWarnings([]);
    }
  }, [rule.data, selection]);

  const dirty = Boolean(draft && original && !sameRule(draft, original));

  // Несохранённые изменения: предупреждение при уходе со страницы и при закрытии вкладки
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  const guard = useCallback(
    (action: () => void) => {
      if (!dirty) return action();
      setConfirm({ title: 'Изменения не сохранены', text: 'Если продолжить, правки в правиле пропадут.', label: 'Не сохранять', danger: true, action });
    },
    [dirty],
  );

  const open = (next: Selection) =>
    guard(() => {
      setServerErrors({});
      setWarnings([]);
      setSelection(next);
      if (next.kind === 'new') {
        const input = emptyRule(profileId);
        setDraft(input);
        setOriginal(input);
      } else if (next.kind === 'none') {
        setDraft(null);
        setOriginal(null);
      } else {
        setDraft(null);
        setOriginal(null);
      }
    });

  const refreshLists = () => {
    void qc.invalidateQueries({ queryKey: keys.rules(profileId) });
    void qc.invalidateQueries({ queryKey: keys.profiles });
  };

  const save = useMutation({
    mutationFn: async () => {
      if (!draft) throw new Error('нет черновика');
      if (selection.kind === 'rule' && rule.data) {
        const patch: Partial<RuleInput> & { version: number } = { version: rule.data.version };
        for (const key of Object.keys(draft) as (keyof RuleInput)[]) {
          if (JSON.stringify(draft[key]) !== JSON.stringify(original?.[key])) (patch as Record<string, unknown>)[key] = draft[key];
        }
        return api.updateRule(rule.data.id, patch);
      }
      return api.createRule({ ...draft, profile_id: profileId });
    },
    onSuccess: (saved: Rule) => {
      qc.setQueryData(keys.rule(saved.id), saved);
      refreshLists();
      const input = ruleToInput(saved);
      setOriginal(input);
      setDraft(input);
      setServerErrors({});
      setWarnings(saved.warnings ?? []);
      setSelection({ kind: 'rule', id: saved.id });
      toast.show(selection.kind === 'new' ? `Правило ${saved.code} создано. Оно применится к следующим пакетам` : 'Правило сохранено. Изменения применятся к следующим пакетам');
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 422) {
        setServerErrors(e.fields);
        toast.show(e.message, { tone: 'error' });
      } else if (e instanceof ApiError && e.code === 'version_conflict') {
        const current = e.details.current as Rule | undefined;
        if (current) qc.setQueryData(keys.rule(current.id), current);
        toast.show('Правило уже изменил другой методолог. Загружена актуальная версия — повторите правку', { tone: 'error', duration: 7000 });
      } else {
        toast.error(e);
      }
    },
  });

  const remove = async () => {
    if (selection.kind !== 'rule') return;
    try {
      await api.deleteRule(selection.id);
      refreshLists();
      setSelection({ kind: 'none' });
      setDraft(null);
      setOriginal(null);
      toast.show('Правило удалено. Старые находки сохранят его код и название');
    } catch (e) {
      toast.error(e);
    }
  };

  const toggle = async (item: RuleListItem, enabled: boolean) => {
    const key = [...keys.rules(profileId), { search, kind }];
    qc.setQueryData<RuleListItem[]>(key, (list) => list?.map((r) => (r.id === item.id ? { ...r, enabled } : r)));
    try {
      const current = await api.getRule(item.id);
      const saved = await api.updateRule(item.id, { enabled, version: current.version });
      qc.setQueryData(keys.rule(saved.id), saved);
      if (selection.kind === 'rule' && selection.id === item.id && !dirty) {
        const input = ruleToInput(saved);
        setDraft(input);
        setOriginal(input);
      }
      void qc.invalidateQueries({ queryKey: keys.profiles });
      toast.show(enabled ? `Правило ${item.code} включено` : `Правило ${item.code} выключено`);
    } catch (e) {
      qc.setQueryData<RuleListItem[]>(key, (list) => list?.map((r) => (r.id === item.id ? { ...r, enabled: !enabled } : r)));
      toast.error(e);
    }
  };

  const makeDefault = async (p: RuleProfile) => {
    try {
      await api.updateProfile(p.id, { is_default: true });
      void qc.invalidateQueries({ queryKey: keys.profiles });
      toast.show(`«${p.name}» — профиль по умолчанию для новых пакетов`);
    } catch (e) {
      toast.error(e);
    }
  };

  const deleteProfile = async (p: RuleProfile) => {
    try {
      await api.deleteProfile(p.id);
      await qc.invalidateQueries({ queryKey: keys.profiles });
      setProfileId('');
      setSelection({ kind: 'none' });
      toast.show(`Профиль «${p.name}» удалён`);
    } catch (e) {
      toast.error(e);
    }
  };

  const items = useMemo(() => (rules.data ?? []).filter((r) => !level || r.level === level), [rules.data, level]);
  const currentProfile = profiles.data?.find((p) => p.id === profileId);
  const editorOpen = selection.kind !== 'none';

  if (profiles.isError) {
    return (
      <div className="page">
        <ErrorState error={profiles.error} onRetry={() => void profiles.refetch()} />
      </div>
    );
  }

  return (
    <div className="page page--wide">
      <header className="page__header">
        <div className="page__heading">
          <h1>Правила</h1>
          <p className="page__subtitle">Чек-лист рисков. Изменения применяются только к новым пакетам.</p>
        </div>
        <div className="page__actions">
          <Button variant="primary" size="lg" onClick={() => open({ kind: 'new' })} disabled={!profileId}>
            <Plus size={18} /> Новое правило
          </Button>
        </div>
      </header>

      <div className="profiles" role="tablist" aria-label="Профили правил">
        {!profiles.data && [0, 1, 2].map((i) => <Skeleton key={i} width={160} height={34} />)}
        {profiles.data?.map((p) => (
          <div key={p.id} className={p.id === profileId ? 'profile-chip profile-chip--active' : 'profile-chip'}>
            <button type="button" role="tab" aria-selected={p.id === profileId} className="profile-chip__main" onClick={() => p.id !== profileId && guard(() => { setProfileId(p.id); setSelection({ kind: 'none' }); setDraft(null); setOriginal(null); })}>
              {p.name}
              {p.is_default && <span className="profile-chip__default">по умолчанию</span>}
            </button>
            {p.id === profileId && (
              <Menu
                trigger={(props) => (
                  <button type="button" className="profile-chip__menu" aria-label={`Действия с профилем ${p.name}`} {...props}>
                    <MoreHorizontal size={16} />
                  </button>
                )}
                align="left"
                items={[
                  { label: 'Переименовать', icon: <Pencil size={16} />, onClick: () => setProfileDialog(p) },
                  { label: 'Сделать профилем по умолчанию', icon: <Star size={16} />, disabled: p.is_default, onClick: () => void makeDefault(p) },
                  'separator',
                  {
                    label: p.is_default ? 'Профиль по умолчанию удалить нельзя' : 'Удалить профиль',
                    icon: <Trash2 size={16} />,
                    danger: true,
                    disabled: p.is_default,
                    onClick: () =>
                      setConfirm({
                        title: 'Удалить профиль?',
                        text: `Профиль «${p.name}» и его правила будут удалены. Уже проверенные пакеты сохранят имя профиля и находки.`,
                        label: 'Удалить профиль',
                        danger: true,
                        action: () => void deleteProfile(p),
                      }),
                  },
                ]}
              />
            )}
          </div>
        ))}
        {profiles.data && (
          <button type="button" className="chip chip--dashed" onClick={() => setProfileDialog('new')}>
            <Plus size={14} /> Профиль
          </button>
        )}
      </div>

      <div className={editorOpen ? 'rules-layout rules-layout--editor' : 'rules-layout'}>
        <div className="rules-layout__list">
          <div className="filters" style={{ marginBottom: 14 }}>
            <label className="input-icon" style={{ flex: 1, minWidth: 200 }}>
              <Search size={17} />
              <input className="input" type="search" placeholder="Код или название" aria-label="Поиск правила по коду или названию" value={searchInput} onChange={(e) => setSearchInput(e.target.value)} />
            </label>
            <label className="filters__select">
              <span>Тип</span>
              <select className="select" value={kind} onChange={(e) => setKind(e.target.value as RuleKind | '')}>
                <option value="">Все</option>
                {(Object.keys(RULE_KIND_LABELS) as RuleKind[]).map((k) => (
                  <option key={k} value={k}>
                    {RULE_KIND_LABELS[k]}
                  </option>
                ))}
              </select>
            </label>
            <label className="filters__select">
              <span>Уровень</span>
              <select className="select" value={level} onChange={(e) => setLevel(e.target.value as Level | '')}>
                <option value="">Любой</option>
                <option value="critical">Критично</option>
                <option value="warning">Внимание</option>
              </select>
            </label>
          </div>

          <div className="card table-wrap">
            {rules.isPending ? (
              <ListSkeleton rows={6} height={44} />
            ) : rules.isError ? (
              <ErrorState error={rules.error} onRetry={() => void rules.refetch()} />
            ) : !items.length ? (
              search || kind || level ? (
                <EmptyState icon={<SearchX size={28} />} title="Правил не найдено" actions={<Button onClick={() => { setSearchInput(''); setKind(''); setLevel(''); }}>Сбросить фильтры</Button>} />
              ) : (
                <EmptyState
                  icon={<ListChecks size={28} />}
                  title={`В профиле «${currentProfile?.name ?? ''}» пока нет правил`}
                  text="Добавьте первое правило: опишите риск словами и дайте ИИ инструкцию, что искать."
                  actions={<Button variant="primary" onClick={() => open({ kind: 'new' })}><Plus size={16} /> Новое правило</Button>}
                />
              )
            ) : (
              <table className="table table--clickable rules-table">
                <thead>
                  <tr>
                    <th style={{ width: 64 }}>Вкл.</th>
                    <th style={{ width: 64 }}>Код</th>
                    <th>Правило</th>
                    <th>Уровень</th>
                    <th className="num" title="Сколько раз правило сработало за 30 дней">За 30 дней</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((r) => (
                    <tr
                      key={r.id}
                      className={[selection.kind === 'rule' && selection.id === r.id && 'is-selected', !r.enabled && 'is-disabled'].filter(Boolean).join(' ')}
                      tabIndex={0}
                      onClick={() => open({ kind: 'rule', id: r.id })}
                      onKeyDown={(e) => e.key === 'Enter' && open({ kind: 'rule', id: r.id })}
                    >
                      <td onClick={(e) => e.stopPropagation()}>
                        <Toggle checked={r.enabled} label={`Правило ${r.code} ${r.enabled ? 'включено' : 'выключено'}`} onChange={(v) => void toggle(r, v)} />
                      </td>
                      <td className="mono nowrap">{r.code}</td>
                      <td>
                        <div className="cell-title">{r.title}</div>
                        <div className="cell-sub">
                          {RULE_KIND_LABELS[r.kind]}
                          {!r.enabled && ' · выключено'}
                        </div>
                      </td>
                      <td>
                        <LevelBadge level={r.level} small />
                      </td>
                      <td className="num">{r.hits_30d || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {editorOpen &&
          (draft ? (
            <RuleEditor
              key={selection.kind === 'rule' ? selection.id : 'new'}
              rule={selection.kind === 'rule' ? (rule.data ?? null) : null}
              draft={draft}
              onChange={(d) => {
                setDraft(d);
                if (Object.keys(serverErrors).length) setServerErrors({});
              }}
              onSave={() => save.mutate()}
              onCancel={() => open({ kind: 'none' })}
              onDelete={() =>
                setConfirm({
                  title: 'Удалить правило?',
                  text: `Правило ${draft.code} больше не будет проверяться в новых пакетах. Находки в уже проверенных пакетах сохранятся.`,
                  label: 'Удалить правило',
                  danger: true,
                  action: () => void remove(),
                })
              }
              saving={save.isPending}
              serverErrors={serverErrors}
              warnings={warnings}
            />
          ) : rule.isError ? (
            <div className="card rule-editor">
              <ErrorState error={rule.error} onRetry={() => void rule.refetch()} />
            </div>
          ) : (
            <div className="card rule-editor">
              <ListSkeleton rows={6} height={40} />
            </div>
          ))}
      </div>

      {profileDialog && profiles.data && (
        <ProfileDialog
          profile={profileDialog === 'new' ? null : profileDialog}
          profiles={profiles.data}
          onClose={() => setProfileDialog(null)}
          onSaved={(p) => {
            setProfileDialog(null);
            if (profileDialog === 'new') {
              setProfileId(p.id);
              setSelection({ kind: 'none' });
              toast.show(`Профиль «${p.name}» создан`);
            } else {
              toast.show('Профиль переименован');
            }
          }}
        />
      )}
      {confirm && (
        <ConfirmDialog
          title={confirm.title}
          text={confirm.text}
          confirmLabel={confirm.label}
          danger={confirm.danger}
          onConfirm={() => {
            const action = confirm.action;
            setConfirm(null);
            action();
          }}
          onClose={() => setConfirm(null)}
        />
      )}
      {blocker.state === 'blocked' && (
        <ConfirmDialog
          title="Уйти без сохранения?"
          text="В правиле есть несохранённые изменения. Если уйти со страницы, они пропадут."
          confirmLabel="Уйти"
          danger
          onConfirm={() => blocker.proceed()}
          onClose={() => blocker.reset()}
        />
      )}
    </div>
  );
}
