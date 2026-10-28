import { AlertCircle, Check, Download, FolderOpen, MoreHorizontal, Pencil, Plus, Search, SearchX, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { useBatches } from '../api/queries';
import type { BatchListItem, BatchListParams, BatchStatusFilter, LevelFilter, OwnerFilter, PeriodFilter } from '../api/types';
import { DeleteBatchDialog, RenameBatchDialog } from '../components/BatchDialogs';
import { EmptyState, ErrorState, ListSkeleton, Menu } from '../components/feedback';
import { Button, FindingsBadges, LevelDot, Segmented, Spinner } from '../components/ui';
import { countLabel, formatDate, formatTime } from '../lib/format';
import { worstLevelLabel } from '../lib/labels';
import { useDebounced } from '../lib/useDebounced';
import { useExport } from '../lib/useExport';

const PAGE_SIZE = 20;

const STATUS_OPTIONS: { value: BatchStatusFilter; label: string }[] = [
  { value: 'all', label: 'Все' },
  { value: 'processing', label: 'Обрабатывается' },
  { value: 'done', label: 'Готово' },
  { value: 'error', label: 'Ошибка' },
];

function readParams(sp: URLSearchParams): Required<Omit<BatchListParams, 'page_size'>> {
  return {
    search: sp.get('search') ?? '',
    status: (sp.get('status') as BatchStatusFilter) || 'all',
    level: (sp.get('level') as LevelFilter) || 'any',
    period: (sp.get('period') as PeriodFilter) || '30d',
    owner: (sp.get('owner') as OwnerFilter) || 'all',
    page: Math.max(1, Number(sp.get('page')) || 1),
  };
}

const DEFAULTS = { search: '', status: 'all', level: 'any', period: '30d', owner: 'all', page: 1 };

export function BatchStatusCell({ batch }: { batch: Pick<BatchListItem, 'status' | 'error_documents_count'> }) {
  if (batch.status === 'queued') return <span className="status status--queued">В очереди</span>;
  if (batch.status === 'processing') {
    return (
      <span className="status status--processing">
        <Spinner /> Обрабатывается
      </span>
    );
  }
  if (batch.status === 'partial_error') {
    return (
      <span className="status status--error">
        <AlertCircle size={16} />
        Ошибка в {countLabel(batch.error_documents_count, ['файле', 'файлах', 'файлах'])}
      </span>
    );
  }
  return (
    <span className="status status--done">
      <Check size={16} /> Готово
    </span>
  );
}

export function BatchesPage() {
  const [sp, setSp] = useSearchParams();
  const params = readParams(sp);
  const [searchInput, setSearchInput] = useState(params.search);
  const debouncedSearch = useDebounced(searchInput);
  const navigate = useNavigate();
  const exporter = useExport();
  const [renaming, setRenaming] = useState<BatchListItem | null>(null);
  const [deleting, setDeleting] = useState<BatchListItem | null>(null);

  const update = (patch: Partial<typeof params>) => {
    const next = { ...params, page: 1, ...patch };
    const out = new URLSearchParams();
    for (const [k, v] of Object.entries(next)) {
      if (v !== DEFAULTS[k as keyof typeof DEFAULTS]) out.set(k, String(v));
    }
    setSp(out, { replace: true });
  };

  useEffect(() => {
    if (debouncedSearch !== params.search) update({ search: debouncedSearch });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch]);

  const query = useBatches({ ...params, page_size: PAGE_SIZE });
  const data = query.data;
  const filtered = params.search || params.status !== 'all' || params.level !== 'any' || params.owner !== 'all' || params.period !== '30d';
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  const open = (b: BatchListItem) =>
    navigate(b.status === 'queued' || b.status === 'processing' ? `/batches/${b.id}/processing` : `/batches/${b.id}`);

  return (
    <div className="page">
      <header className="page__header">
        <div className="page__heading">
          <h1>Пакеты</h1>
          {data && (
            <p className="page__subtitle">
              {countLabel(data.total, ['пакет', 'пакета', 'пакетов'])}
              {data.processing_count > 0 && ` · ${data.processing_count} в обработке`}
            </p>
          )}
        </div>
        <div className="page__actions">
          <Link to="/upload" className="btn btn--primary btn--lg">
            <Plus size={18} /> Загрузить пакет
          </Link>
        </div>
      </header>

      <div className="filters">
        <label className="input-icon filters__search">
          <Search size={17} />
          <input
            className="input"
            type="search"
            placeholder="Название пакета или файла"
            aria-label="Поиск по названию пакета или файла"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </label>
        <Segmented label="Статус" value={params.status} options={STATUS_OPTIONS} onChange={(status) => update({ status })} />
        <label className="filters__select">
          <span>Уровень</span>
          <select className="select" value={params.level} onChange={(e) => update({ level: e.target.value as LevelFilter })}>
            <option value="any">Любой</option>
            <option value="critical">Критично</option>
            <option value="warning">Внимание</option>
            <option value="ok">Без рисков</option>
          </select>
        </label>
        <label className="filters__select">
          <span>Период</span>
          <select className="select" value={params.period} onChange={(e) => update({ period: e.target.value as PeriodFilter })}>
            <option value="7d">7 дней</option>
            <option value="30d">30 дней</option>
            <option value="all">Всё время</option>
          </select>
        </label>
        <Segmented
          label="Чьи пакеты"
          value={params.owner}
          options={[
            { value: 'me', label: 'Мои' },
            { value: 'all', label: 'Все' },
          ]}
          onChange={(owner) => update({ owner })}
        />
      </div>

      <div className="card table-wrap">
        {query.isPending ? (
          <ListSkeleton rows={6} height={44} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : data && data.items.length === 0 ? (
          filtered ? (
            <EmptyState
              icon={<SearchX size={28} />}
              title="Ничего не найдено"
              text="Попробуйте изменить запрос или фильтры."
              actions={
                <Button
                  onClick={() => {
                    setSearchInput('');
                    setSp(new URLSearchParams(), { replace: true });
                  }}
                >
                  Сбросить фильтры
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={<FolderOpen size={28} />}
              title="Пока нет пакетов"
              text="Загрузите пакет закупочной документации — система проверит каждый документ по чек-листу рисков."
              actions={
                <Link to="/upload" className="btn btn--primary">
                  <Plus size={16} /> Загрузить первый пакет
                </Link>
              }
            />
          )
        ) : (
          <table className="table table--clickable batches-table">
            <thead>
              <tr>
                <th style={{ width: 36 }}>
                  <span className="visually-hidden">Уровень</span>
                </th>
                <th>Пакет</th>
                <th>Загружен</th>
                <th>Автор</th>
                <th>Профиль правил</th>
                <th className="num">Документов</th>
                <th>Находки</th>
                <th>Статус</th>
                <th style={{ width: 48 }}>
                  <span className="visually-hidden">Действия</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data?.items.map((b) => (
                <tr
                  key={b.id}
                  onClick={() => open(b)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === 'Enter' && open(b)}
                  aria-label={`${b.name}: ${worstLevelLabel(b.worst_level)}`}
                >
                  <td>
                    <span className="tooltip" data-tip={worstLevelLabel(b.worst_level)}>
                      <LevelDot level={b.worst_level} large />
                    </span>
                  </td>
                  <td>
                    <div className="cell-title">{b.name}</div>
                    {b.subject && <div className="cell-sub">{b.subject}</div>}
                  </td>
                  <td className="nowrap">
                    <div>{formatDate(b.created_at)}</div>
                    <div className="cell-sub">{formatTime(b.created_at)}</div>
                  </td>
                  <td>{b.author?.full_name ?? '—'}</td>
                  <td>{b.profile?.short_name ?? '—'}</td>
                  <td className="num">{b.documents_count}</td>
                  <td>
                    {b.status === 'queued' || b.status === 'processing' ? (
                      <span className="subtle">—</span>
                    ) : (
                      <FindingsBadges findings={b.findings} worst={b.worst_level} small />
                    )}
                  </td>
                  <td>
                    <BatchStatusCell batch={b} />
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <Menu
                      trigger={(props) => (
                        <button type="button" className="icon-btn" aria-label={`Действия с пакетом ${b.name}`} {...props}>
                          {exporter.isPending(`batch:${b.id}`) ? <Spinner /> : <MoreHorizontal size={18} />}
                        </button>
                      )}
                      items={[
                        {
                          label: 'Скачать пакет',
                          icon: <Download size={16} />,
                          disabled: b.status === 'queued' || b.status === 'processing' || exporter.isPending(`batch:${b.id}`),
                          onClick: () => void exporter.downloadBatch(b.id),
                        },
                        { label: 'Переименовать', icon: <Pencil size={16} />, onClick: () => setRenaming(b) },
                        'separator',
                        { label: 'Удалить', icon: <Trash2 size={16} />, danger: true, onClick: () => setDeleting(b) },
                      ]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {data && data.total > 0 && (
        <div className="list-footer">
          <span className="muted">
            Показано {(params.page - 1) * PAGE_SIZE + 1}–{Math.min(params.page * PAGE_SIZE, data.total)} из {data.total}
          </span>
          {pages > 1 && (
            <div className="pagination">
              <Button disabled={params.page <= 1} onClick={() => update({ page: params.page - 1 })}>
                Назад
              </Button>
              {Array.from({ length: pages }, (_, i) => i + 1).map((p) => (
                <Button
                  key={p}
                  className={p === params.page ? 'pagination__page pagination__page--active' : 'pagination__page'}
                  aria-current={p === params.page ? 'page' : undefined}
                  onClick={() => update({ page: p })}
                >
                  {p}
                </Button>
              ))}
              <Button disabled={params.page >= pages} onClick={() => update({ page: params.page + 1 })}>
                Вперёд
              </Button>
            </div>
          )}
        </div>
      )}

      {renaming && <RenameBatchDialog batch={renaming} onClose={() => setRenaming(null)} />}
      {deleting && <DeleteBatchDialog batch={deleting} onClose={() => setDeleting(null)} />}
    </div>
  );
}
