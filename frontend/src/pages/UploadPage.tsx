import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronDown, ChevronUp, Info, Upload, X } from 'lucide-react';
import { type DragEvent, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { api } from '../api/endpoints';
import { ApiError } from '../api/http';
import { keys, useConfig, useProfiles } from '../api/queries';
import type { UploadedFile } from '../api/types';
import { ErrorState, errorMessage, ListSkeleton, useToast } from '../components/feedback';
import { Breadcrumbs, Button, Field, FileTypeIcon, ProgressBar } from '../components/ui';
import { countLabel, fileExtension, formatBytes, formatDate, formatMegabytes } from '../lib/format';

interface PendingUpload {
  id: number;
  files: File[];
  loaded: number;
  total: number;
  error?: string;
}

const DEFAULT_EXTENSIONS = ['zip', 'pdf', 'docx', 'doc', 'rtf'];
const DEFAULT_MAX = 100 * 1024 * 1024;

function isCheckable(f: UploadedFile) {
  return f.check === 'ok' || f.check === 'encrypted' || (f.archive_entries ?? []).some((e) => e.check === 'ok');
}

export function UploadPage() {
  const [sp, setSp] = useSearchParams();
  const draftId = sp.get('draft');
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const config = useConfig();
  const profiles = useProfiles();
  const inputRef = useRef<HTMLInputElement>(null);
  const draftPromise = useRef<Promise<string> | null>(null);
  const seq = useRef(0);

  // Черновик, созданный на этой странице, не перечитываем: ответ мог бы затереть только что загруженные файлы
  const [createdId, setCreatedId] = useState<string | null>(null);
  const draft = useQuery({
    queryKey: keys.batch(draftId ?? 'none'),
    queryFn: () => api.getBatch(draftId!),
    enabled: Boolean(draftId) && draftId !== createdId,
    staleTime: Infinity,
  });

  const [files, setFiles] = useState<UploadedFile[]>([]);
  const [totalBytes, setTotalBytes] = useState(0);
  const [pending, setPending] = useState<PendingUpload[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [dragging, setDragging] = useState(false);
  const [name, setName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [profileId, setProfileId] = useState('');
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  // Черновик из адреса: после перезагрузки страницы файлы на месте
  useEffect(() => {
    if (!draft.data) return;
    if (draft.data.status !== 'draft') {
      navigate(`/batches/${draft.data.id}/processing`, { replace: true });
      return;
    }
    setFiles(draft.data.files ?? []);
    setTotalBytes(draft.data.total_bytes ?? 0);
    if (draft.data.name) setName((n) => n || draft.data!.name);
  }, [draft.data, navigate]);

  useEffect(() => {
    if (!profileId && profiles.data?.length) setProfileId((profiles.data.find((p) => p.is_default) ?? profiles.data[0]).id);
  }, [profiles.data, profileId]);

  // Название по умолчанию — из имени первого архива, иначе по дате
  useEffect(() => {
    if (nameTouched) return;
    const zip = files.find((f) => fileExtension(f.filename) === 'zip');
    if (zip) setName(zip.filename.replace(/\.zip$/i, ''));
    else if (files.length && !name) setName(`Пакет от ${formatDate(new Date().toISOString())}`);
  }, [files, nameTouched, name]);

  const allowed = config.data?.upload.allowed_extensions ?? DEFAULT_EXTENSIONS;
  const maxBytes = config.data?.upload.max_batch_bytes ?? DEFAULT_MAX;
  const pendingBytes = pending.filter((p) => !p.error).reduce((s, p) => s + p.total, 0);
  const overLimit = totalBytes + pendingBytes > maxBytes || pending.some((p) => p.error && p.error.includes('100 МБ'));
  const uploading = pending.some((p) => !p.error);
  const checkable = files.filter(isCheckable);
  const selectedProfile = profiles.data?.find((p) => p.id === profileId);

  const ensureDraft = () => {
    if (draftId) return Promise.resolve(draftId);
    draftPromise.current ??= api.createDraft().then((d) => {
      setCreatedId(d.id);
      setSp({ draft: d.id }, { replace: true });
      return d.id;
    });
    return draftPromise.current;
  };

  const addFiles = async (list: FileList | File[]) => {
    const picked = [...list].filter((f) => f.size >= 0);
    if (!picked.length) return;
    const id = ++seq.current;
    const total = picked.reduce((s, f) => s + f.size, 0);
    setPending((p) => [...p, { id, files: picked, loaded: 0, total }]);
    setStartError(null);
    try {
      const batchId = await ensureDraft();
      const res = await api.uploadFiles(batchId, picked, (loaded) => setPending((p) => p.map((x) => (x.id === id ? { ...x, loaded } : x))));
      setFiles(res.files);
      setTotalBytes(res.total_bytes);
      setPending((p) => p.filter((x) => x.id !== id));
    } catch (e) {
      const message =
        e instanceof ApiError && e.code === 'batch_too_large'
          ? 'Не загружено: пакет превысит 100 МБ. Уберите лишние файлы или разделите пакет'
          : errorMessage(e);
      setPending((p) => p.map((x) => (x.id === id ? { ...x, error: message } : x)));
    }
  };

  const removeFile = async (fileId: string) => {
    if (!draftId) return;
    try {
      const res = await api.removeFile(draftId, fileId);
      setFiles(res.files);
      setTotalBytes(res.total_bytes);
    } catch (e) {
      toast.error(e);
    }
  };

  const start = async () => {
    if (!draftId) return;
    setStarting(true);
    setStartError(null);
    try {
      await api.startBatch(draftId, name.trim(), profileId);
      void qc.invalidateQueries({ queryKey: keys.batches() });
      qc.removeQueries({ queryKey: keys.batch(draftId) });
      navigate(`/batches/${draftId}/processing`);
    } catch (e) {
      setStartError(errorMessage(e));
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (draftId) {
      try {
        await api.deleteBatch(draftId);
      } catch {
        // Черновик удалится сам через 24 часа
      }
    }
    navigate('/batches');
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) void addFiles(e.dataTransfer.files);
  };

  const disabledReason = useMemo(() => {
    if (uploading) return 'Дождитесь окончания загрузки файлов';
    if (overLimit) return 'Пакет больше 100 МБ — уберите лишние файлы';
    if (!files.length) return 'Добавьте файлы пакета';
    if (!checkable.length) return 'Среди файлов нет ни одного, который можно проверить';
    if (!name.trim()) return 'Введите название пакета';
    if (!profileId) return 'Выберите профиль правил';
    return null;
  }, [uploading, overLimit, files.length, checkable.length, name, profileId]);

  if (draftId && draft.isError) {
    return (
      <div className="page">
        <ErrorState
          error={draft.error}
          title="Черновик пакета не найден"
          onRetry={() => setSp({}, { replace: true })}
        />
      </div>
    );
  }

  const totalShown = totalBytes + pendingBytes;

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <Breadcrumbs items={[{ label: 'Пакеты', to: '/batches' }, { label: 'Загрузка пакета' }]} />
          <h1>Загрузка пакета</h1>
          <p className="page__subtitle">Система извлечёт текст, распознает сканы и проверит каждый документ по выбранному профилю правил.</p>
        </div>
      </header>

      <div className="upload">
        <div className="upload__main">
          <div
            className={dragging ? 'dropzone dropzone--active' : 'dropzone'}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false);
            }}
            onDrop={onDrop}
          >
            <div className="dropzone__icon">
              <Upload size={24} />
            </div>
            <div className="dropzone__title">Перетащите файлы или zip-архив сюда</div>
            <div className="row">
              <span className="muted">или</span>
              <Button onClick={() => inputRef.current?.click()}>Выбрать файлы</Button>
            </div>
            <div className="dropzone__hint">
              {allowed.join(', ')} · до {formatMegabytes(maxBytes)} МБ на пакет · можно добавлять файлы несколько раз
            </div>
            <input
              ref={inputRef}
              type="file"
              multiple
              hidden
              accept={allowed.map((e) => `.${e}`).join(',')}
              onChange={(e) => {
                if (e.target.files) void addFiles(e.target.files);
                e.target.value = '';
              }}
            />
          </div>

          {draftId && draftId !== createdId && draft.isPending ? (
            <div className="card section-gap">
              <ListSkeleton rows={2} />
            </div>
          ) : (
            (files.length > 0 || pending.length > 0) && (
              <div className="card section-gap">
                <div className="card__header">
                  <h2>Выбранные файлы · {files.length + pending.reduce((s, p) => s + (p.error ? 0 : p.files.length), 0)}</h2>
                  <div className="spacer" />
                  <span className={overLimit ? 'text-critical tabular' : 'muted tabular'}>
                    {formatMegabytes(totalShown)} МБ из {formatMegabytes(maxBytes)}
                  </span>
                  <div style={{ width: 140 }} className={overLimit ? 'progress-critical' : undefined}>
                    <ProgressBar value={(totalShown / maxBytes) * 100} label="Объём пакета" />
                  </div>
                </div>
                <ul className="file-list">
                  {files.map((f) => {
                    const bad = f.check === 'unsupported' || f.check === 'corrupted';
                    const warn = f.check === 'encrypted';
                    const entries = f.archive_entries ?? [];
                    const isOpen = expanded.has(f.id);
                    return (
                      <li key={f.id} className={bad ? 'file-row file-row--bad' : 'file-row'}>
                        <div className="file-row__main">
                          <FileTypeIcon filename={f.filename} bad={bad} small />
                          <div className="file-row__text">
                            <div className="file-row__name">{f.filename}</div>
                            <div className={bad ? 'file-row__meta text-critical' : warn ? 'file-row__meta text-warning' : 'file-row__meta'}>
                              {formatBytes(f.size_bytes)}
                              {entries.length > 0 && ` · ${countLabel(entries.length, ['файл', 'файла', 'файлов'])} внутри`}
                              {f.message && ` · ${f.message}`}
                            </div>
                          </div>
                          {entries.length > 0 && (
                            <button
                              type="button"
                              className="icon-btn"
                              aria-expanded={isOpen}
                              aria-label={isOpen ? 'Свернуть содержимое архива' : 'Показать содержимое архива'}
                              onClick={() =>
                                setExpanded((s) => {
                                  const next = new Set(s);
                                  if (next.has(f.id)) next.delete(f.id);
                                  else next.add(f.id);
                                  return next;
                                })
                              }
                            >
                              {isOpen ? <ChevronUp size={18} /> : <ChevronDown size={18} />}
                            </button>
                          )}
                          <button type="button" className="icon-btn" aria-label={`Убрать ${f.filename}`} onClick={() => void removeFile(f.id)}>
                            <X size={18} />
                          </button>
                        </div>
                        {isOpen && (
                          <ul className="archive-list">
                            {entries.map((e) => (
                              <li key={e.path} className={e.check !== 'ok' ? 'archive-list__bad' : undefined}>
                                <span>{e.path}</span>
                                <span className="tabular">{e.check === 'ok' ? formatBytes(e.size_bytes) : 'не будет проверен'}</span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    );
                  })}
                  {pending.map((p) => (
                    <li key={`p${p.id}`} className={p.error ? 'file-row file-row--bad' : 'file-row'}>
                      <div className="file-row__main">
                        <FileTypeIcon filename={p.files[0].name} bad={Boolean(p.error)} small />
                        <div className="file-row__text">
                          <div className="file-row__name">
                            {p.files.length === 1 ? p.files[0].name : `${p.files[0].name} и ещё ${countLabel(p.files.length - 1, ['файл', 'файла', 'файлов'])}`}
                          </div>
                          {p.error ? (
                            <div className="file-row__meta text-critical">
                              {formatBytes(p.total)} · {p.error}
                            </div>
                          ) : (
                            <div className="file-row__progress">
                              <ProgressBar value={p.total ? (p.loaded / p.total) * 100 : 50} label="Загрузка файла" />
                              <span className="muted tabular">Загрузка… {formatBytes(p.loaded)} из {formatBytes(p.total)}</span>
                            </div>
                          )}
                        </div>
                        {p.error && (
                          <button type="button" className="icon-btn" aria-label="Убрать из списка" onClick={() => setPending((x) => x.filter((y) => y.id !== p.id))}>
                            <X size={18} />
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            )
          )}
        </div>

        <aside className="card upload__side">
          <div className="card__body stack" style={{ gap: 18 }}>
            <h2>Параметры проверки</h2>
            <Field label="Название пакета" htmlFor="batch-name">
              <input
                id="batch-name"
                className="input"
                value={name}
                placeholder="Например, Закупка_0001"
                onChange={(e) => {
                  setName(e.target.value);
                  setNameTouched(true);
                }}
                maxLength={200}
              />
            </Field>
            <Field
              label="Профиль правил"
              htmlFor="profile"
              hint={
                selectedProfile &&
                `${countLabel(selectedProfile.enabled_rules_count, ['правило', 'правила', 'правил'])}${selectedProfile.is_default ? ' · профиль по умолчанию' : ''}`
              }
            >
              <select id="profile" className="select" value={profileId} onChange={(e) => setProfileId(e.target.value)} disabled={!profiles.data}>
                {profiles.data?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </Field>
            <div className="note">
              <Info size={16} />
              <span>Проверка займёт около 2 минут. Можно закрыть страницу — обработка продолжится, пакет появится в списке «Пакеты».</span>
            </div>
            {startError && (
              <div className="field__error" role="alert">
                {startError}
              </div>
            )}
            <div className="stack" style={{ gap: 10 }}>
              <span className={disabledReason ? 'tooltip' : undefined} data-tip={disabledReason ?? undefined}>
                <Button variant="primary" size="lg" block loading={starting} disabled={Boolean(disabledReason)} onClick={() => void start()}>
                  Начать проверку
                </Button>
              </span>
              <Button size="lg" block onClick={() => void cancel()}>
                Отмена
              </Button>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
