// @ts-nocheck
import React, { useState, useEffect } from 'react';
import { Link } from 'react-router';
import { History, ChevronRight, ChevronDown, Trash2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { ApiClient } from '../../api/client';
import { SkeletonRow } from '../../components/ui/Skeleton';
import { EmptyState } from '../../components/ui/Feedback';
import { ConfirmDialog, type ConfirmState } from '../../shared/ui/ConfirmDialog';
import { formatHistoryDate, formatCountLabel } from '../../shared/lib';
import { useRouteTransitionCoordinator } from '../../routing/RouteTransitionCoordinator';
import { deepSearchPath } from '../../routing/paths';

export type StandardHistoryStatus = 'COMPLETED' | 'FAILED' | 'PENDING' | 'RUNNING';

export type StandardHistoryEntry = {
  id: string;
  title: string;
  subtitle: string;
  kindLabel?: string;
  status: StandardHistoryStatus;
  href?: string;
  onBeforeNavigate?: () => void;
  onOpen?: () => void;
  onDelete: () => void;
  deleteLabel?: string;
};

export const getHistoryStatusLabel = (status: StandardHistoryStatus) => {
  if (status === 'COMPLETED') return 'Concluída';
  if (status === 'FAILED') return 'Falhou';
  return 'Em andamento';
};

export const getHistoryStatusClass = (status: StandardHistoryStatus) => {
  if (status === 'COMPLETED') return 'success';
  if (status === 'FAILED') return 'error';
  return 'warning';
};

export const HistoryToggleButton = ({ title, count, open, onClick }: {
  title: string;
  count: number;
  open: boolean;
  onClick: () => void;
}) => (
  <div className="history-toggle-anchor">
    <button
      type="button"
      className={`btn-icon history-toggle-btn ${open ? 'active' : ''}`}
      title={title}
      onClick={onClick}
      aria-pressed={open}
    >
      <History size={18} aria-hidden="true" />
      {count > 0 && (
        <span className="history-toggle-count">
          {count}
        </span>
      )}
    </button>
  </div>
);

export const HistoryPanelBody = ({
  items,
  loading,
  emptyTitle,
  emptyDescription,
  onDeleteAll,
  historicLabel = 'Histórico de buscas',
  deleteAllLabel = 'Apagar todos',
  showTopBorder = false,
}: {
  items: StandardHistoryEntry[];
  loading?: boolean;
  emptyTitle: string;
  emptyDescription: string;
  onDeleteAll?: () => void;
  deleteAllLabel?: string;
  historicLabel?: string;
  showTopBorder?: boolean;
}) => (
  <div className={`history-panel-body ${showTopBorder ? 'with-border' : ''}`}>
    {(historicLabel || (items.length > 0 && onDeleteAll)) && (
      <div className="history-panel-toolbar" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        {historicLabel ? <span className="history-panel-label" style={{ fontWeight: 600, fontSize: '0.9rem' }}>{historicLabel}</span> : <div />}
        {items.length > 0 && onDeleteAll && (
          <button
            type="button"
            className="btn btn-sm btn-outline history-delete-all-btn"
            onClick={onDeleteAll}
          >
            <Trash2 size={14} /> {deleteAllLabel}
          </button>
        )}
      </div>
    )}
    {loading ? (
      <div className="history-list" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <SkeletonRow />
        <SkeletonRow />
        <SkeletonRow />
      </div>
    ) : items.length === 0 ? (
      <EmptyState title={emptyTitle} description={emptyDescription} />
    ) : (
      <div className="history-list">
        {items.map(item => {
          const linkable = typeof item.href === 'string' && item.status === 'COMPLETED';
          const clickable = !linkable && typeof item.onOpen === 'function' && item.status === 'COMPLETED';
          return (
            <div
              key={item.id}
              role={clickable ? 'button' : undefined}
              tabIndex={clickable ? 0 : undefined}
              className={`history-row ${linkable || clickable ? 'is-clickable' : ''}`}
              onClick={clickable ? item.onOpen : undefined}
              onKeyDown={clickable ? (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  item.onOpen?.();
                }
              } : undefined}
            >
              {linkable ? (
                <Link
                  className="history-row-link"
                  to={item.href!}
                  onClick={(event) => {
                    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
                      item.onBeforeNavigate?.();
                    }
                  }}
                >
                  <div className="history-row-main">
                    <strong className="history-row-title">{item.title}</strong>
                    <span className="history-row-subtitle">{item.subtitle}</span>
                  </div>
                </Link>
              ) : (
                <div className="history-row-main">
                  <strong className="history-row-title">{item.title}</strong>
                  <span className="history-row-subtitle">{item.subtitle}</span>
                </div>
              )}
              <div className="history-row-meta">
                {item.kindLabel && (
                  <span className="history-pill history-pill-kind">{item.kindLabel}</span>
                )}
                <span className={`history-pill history-pill-status ${getHistoryStatusClass(item.status)}`}>
                  {(item.status === 'PENDING' || item.status === 'RUNNING') && (
                    <RefreshCw className="animate-spin" size={10} aria-hidden="true" />
                  )}
                  {getHistoryStatusLabel(item.status)}
                </span>
                <button
                  type="button"
                  className="btn-icon text-error history-delete-btn"
                  aria-label={item.deleteLabel || 'Excluir do histórico'}
                  onClick={(event) => {
                    event.stopPropagation();
                    item.onDelete();
                  }}
                >
                  <Trash2 size={15} aria-hidden="true" />
                </button>
              </div>
            </div>
          );
        })}
      </div>
    )}
  </div>
);

export const HistoryList = ({ type, refreshKey, collapsed: collapsedProp, onToggleCollapsed, onCountChange, hideHeader = false }: {
  type: 'search' | 'cross';
  onReopen: (jobId: string) => void;
  refreshKey: number;
  /** Optional controlled collapsed state — when provided (with onToggleCollapsed), an external
   * trigger (e.g. the top-right History icon) drives the panel instead of the internal header button. */
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  /** Reports the type-filtered item count upward so an external badge can mirror it without a second fetch. */
  onCountChange?: (count: number) => void;
  /** Hides the internal header bar: the top-right History icon is the ONLY trigger; collapsed → renders nothing
   * (but stays mounted so the count fetch keeps feeding the icon badge). */
  hideHeader?: boolean;
}) => {
  const transition = useRouteTransitionCoordinator();
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [collapsedState, setCollapsedState] = useState(true);
  const collapsed = collapsedProp !== undefined ? collapsedProp : collapsedState;
  const setCollapsed = (updater: (c: boolean) => boolean) => {
    if (onToggleCollapsed) { onToggleCollapsed(); return; }
    setCollapsedState(updater);
  };
  const [deleteTick, setDeleteTick] = useState(0);
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);

  useEffect(() => {
    let cancelled = false;
    setTimeout(() => { if (!cancelled) setLoading(true); }, 0);
    ApiClient.getHistoryList()
      .then(all => { if (!cancelled) setItems(all.filter((h: any) => h.type === type)); })
      .catch(() => { if (!cancelled) setItems([]); })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [deleteTick, refreshKey, type]);

  useEffect(() => {
    onCountChange?.(items.length);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.length]);

  const requestDelete = (jobId: string) => {
    setConfirmState({
      message: 'Excluir esta busca do histórico? Esta ação é permanente.',
      action: () => {
        ApiClient.deleteHistory(jobId)
          .then(() => setDeleteTick(t => t + 1))
          .catch(() => toast.error('Erro ao excluir entrada do histórico.'));
      },
    });
  };

  const handleDeleteAll = () => {
    setConfirmState({
      message: `Apagar todas as ${items.length} buscas deste histórico? Esta ação é permanente.`,
      action: async () => {
        const results = await Promise.allSettled(items.map((i: any) => ApiClient.deleteHistory(i.job_id)));
        const failed = results.filter(r => r.status === 'rejected').length;
        if (failed > 0) toast.error(`${failed} entrada(s) não puderam ser excluídas.`);
        setDeleteTick(t => t + 1);
      },
    });
  };

  const getTitle = (item: any) => {
    if (type === 'cross') return item.target_sku || item.query || 'Busca por SKU';
    const brands: string[] = Array.isArray(item.brands) ? item.brands : [];
    const first2 = brands.slice(0, 2).join(', ');
    const countLabel = brands.length > 0 ? formatCountLabel(brands.length, 'marca', 'marcas') : '';
    if (!first2) return item.query ? `"${item.query}"` : 'Busca comparativa';
    return countLabel ? `${first2} · ${countLabel}` : first2;
  };

  const filteredCount = items.length;
  const entries: StandardHistoryEntry[] = items.map((item: any) => ({
    id: item.job_id,
    title: getTitle(item),
    subtitle: [
      ...(type === 'search' && item.query ? [`"${item.query}"`] : []),
      formatHistoryDate(item.created_at),
    ].join(' · '),
    kindLabel: type === 'search' ? 'Comparativa' : 'SKU',
    status: (item.status || 'PENDING') as StandardHistoryStatus,
    href: item.status === 'COMPLETED' ? deepSearchPath(type, item.job_id) : undefined,
    onBeforeNavigate: item.status === 'COMPLETED' ? () => {
      transition.captureCurrentEntry(type);
      requestAnimationFrame(() => document.getElementById('main-content')?.focus());
    } : undefined,
    onDelete: () => requestDelete(item.job_id),
    deleteLabel: 'Excluir do histórico',
  }));

  if (hideHeader && collapsed) return null;

  return (
    <div className="history-container">
      <div className="history-panel">
        {!hideHeader && (
          <button
            type="button"
            onClick={() => setCollapsed(c => !c)}
            className="history-panel-header"
            aria-expanded={!collapsed}
          >
            <History size={16} className="history-panel-header-icon" aria-hidden="true" />
            <span className="history-panel-title">Histórico</span>
            {filteredCount > 0 && (
              <span className="history-toggle-count inline">
                {filteredCount}
              </span>
            )}
            {collapsed
              ? <ChevronRight size={16} className="history-panel-header-icon" />
              : <ChevronDown size={16} className="history-panel-header-icon" />
            }
          </button>
        )}

        {!collapsed && (
          <HistoryPanelBody
            items={entries}
            loading={loading}
            emptyTitle="Nenhuma busca ainda"
            emptyDescription="Suas buscas aparecerão aqui automaticamente. Clique em uma entrada concluída para reexibir os resultados sem nova raspagem."
            onDeleteAll={handleDeleteAll}
            showTopBorder={!hideHeader}
          />
        )}
      </div>

      <ConfirmDialog state={confirmState} onClose={() => setConfirmState(null)} cancelLabel="Voltar à revisão" />
    </div>
  );
};
