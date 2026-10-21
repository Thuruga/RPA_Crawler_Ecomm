// @ts-nocheck
import React, { useState, useEffect } from 'react';
import { toast } from 'sonner';
import { useShallow } from 'zustand/react/shallow';
import { XCircle, RefreshCw, Images, Square, Pause, AlertTriangle, Check, ExternalLink } from 'lucide-react';
import { ApiClient } from '../../api/client';
import { Panel } from '../../components/ui/Panel';
import { Button } from '../../components/ui/Button';
import { EmptyState, Status } from '../../components/ui/Feedback';
import { ConfirmDialog, type ConfirmState } from '../../shared/ui/ConfirmDialog';
import { HistoryToggleButton, HistoryPanelBody } from '../history/HistoryList';
import type { StandardHistoryEntry } from '../history/HistoryList';
import { formatHistoryDate, formatCountLabel, isMarketplaceBrand } from '../../shared/lib';
import { useBannerStore, type BannerCandidate, type BannerVideoSlide } from '../../stores/bannerStore';

export const ProtectedBannerImage = ({ runId, banner }: { runId: string, banner: BannerCandidate }) => {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let disposed = false;
    let objectUrl: string | null = null;
    ApiClient.getBannerAssetBlob(runId, banner.banner_id).then(blob => {
      if (disposed) return;
      objectUrl = window.URL.createObjectURL(blob);
      setSrc(objectUrl);
    }).catch(() => {
      if (!disposed) setFailed(true);
    });
    return () => {
      disposed = true;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [runId, banner.banner_id]);
  if (failed) return <div className="banner-image-loading"><XCircle size={22} /><span>Imagem indisponível</span></div>;
  return src
    ? <img src={src} alt={banner.alt_text || `${banner.brand_name} — banner ${banner.slide_order}`} />
    : <div className="banner-image-loading"><RefreshCw className="animate-spin" size={22} /><span>Carregando imagem…</span></div>;
};

export const BannerVideoPreview = ({ video, brandName }: { video: BannerVideoSlide, brandName: string }) => {
  const [failed, setFailed] = useState(false);
  if (!video.source_url || failed) {
    return <div className="banner-image-loading"><XCircle size={22} /><span>Vídeo indisponível</span></div>;
  }
  return (
    <video
      src={video.source_url}
      controls
      muted
      playsInline
      preload="metadata"
      aria-label={video.alt_text || `${brandName} — vídeo ${video.slide_order}`}
      onError={() => setFailed(true)}
    >
      Seu navegador não suporta a reprodução de vídeo.
    </video>
  );
};

export const BannersPage = ({ brands }: { brands: any[] }) => {
  const activeBrands = brands.filter(brand => brand.is_active !== false && !isMarketplaceBrand(brand.brand_key));
  const {
    selectedBrands, starting, activeJobId, run, selectedBannerIds, history, historyLoading,
    setSelectedBrands, initializeBrands, start, retryAssisted, stop, toggleBanner, selectAllBanners,
    clearBanners, approve, loadHistory, reopenHistory, deleteHistory,
  } = useBannerStore(useShallow(state => ({
    selectedBrands: state.selectedBrands,
    starting: state.starting,
    activeJobId: state.activeJobId,
    run: state.run,
    selectedBannerIds: state.selectedBannerIds,
    history: state.history,
    historyLoading: state.historyLoading,
    setSelectedBrands: state.setSelectedBrands,
    initializeBrands: state.initializeBrands,
    start: state.start,
    retryAssisted: state.retryAssisted,
    stop: state.stop,
    toggleBanner: state.toggleBanner,
    selectAllBanners: state.selectAllBanners,
    clearBanners: state.clearBanners,
    approve: state.approve,
    loadHistory: state.loadHistory,
    reopenHistory: state.reopenHistory,
    deleteHistory: state.deleteHistory,
  })));
  const [historyCollapsed, setHistoryCollapsed] = useState(true);
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);

  const requestDeleteHistory = (runId: string) => {
    setConfirmState({
      message: 'Excluir esta extração do histórico? Os arquivos sem outras referências também serão removidos.',
      action: () => void deleteHistory(runId),
    });
  };

  const handleDeleteAllHistory = () => {
    setConfirmState({
      message: `Apagar as ${history.length} extrações deste histórico? Esta ação é permanente.`,
      action: async () => {
        const results = await Promise.allSettled(history.map(item => ApiClient.deleteBannerHistory(item.run_id)));
        const deletedIds = history
          .filter((_, index) => results[index]?.status === 'fulfilled')
          .map(item => item.run_id);
        const failed = results.filter(result => result.status === 'rejected').length;
        if (activeJobId && deletedIds.includes(activeJobId)) {
          useBannerStore.setState({
            activeJobId: null,
            run: null,
            selectedBannerIds: [],
            seenBannerIds: [],
          });
        }
        if (failed > 0) toast.error(`${failed} extração(ões) não puderam ser excluídas.`);
        await loadHistory();
      },
    });
  };

  useEffect(() => {
    initializeBrands(activeBrands.map(brand => brand.brand_key));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brands, initializeBrands]);
  useEffect(() => { void loadHistory(); }, [loadHistory]);

  const running = run?.status === 'RUNNING';
  const busy = starting || running;
  const reviewable = run?.status === 'REVIEW';
  const videoSlides = run?.video_slides || [];
  const progressRows = run ? Object.values(run.brand_progress) : [];
  const processed = progressRows.filter(item => !['PENDING', 'RUNNING', 'WAITING_BROWSER'].includes(item.status)).length;
  const percent = progressRows.length ? Math.round((processed / progressRows.length) * 100) : 0;

  const toggleBrand = (key: string) => setSelectedBrands(
    selectedBrands.includes(key) ? selectedBrands.filter(item => item !== key) : [...selectedBrands, key]
  );
  const statusLabel: Record<string, string> = {
    PENDING: 'Aguardando', RUNNING: 'Extraindo', WAITING_BROWSER: 'Aguardando navegador', COMPLETED: 'Concluída',
    BLOCKED: 'Bloqueada', NO_BANNERS: 'Sem banners',
    FAILED: 'Falhou', CANCELLED: 'Cancelada',
  };
  const handleApprove = () => {
    const hasOnlyVideos = (run?.banners.length || 0) === 0 && (videoSlides.length || 0) > 0;
    if (!selectedBannerIds.length && !hasOnlyVideos) return;
    const msg = selectedBannerIds.length > 0
      ? `Aprovar ${selectedBannerIds.length} banners? Os itens desmarcados serão removidos e esta aprovação não poderá ser alterada.`
      : `Concluir revisão com ${videoSlides.length} vídeo(s) detectado(s)?`;
    setConfirmState({
      message: msg,
      action: () => void approve(),
    });
  };
  const openProtected = (blob: Promise<Blob>) => {
    void ApiClient.openProtectedBlob(blob).catch(error => toast.error(`Erro ao abrir arquivo: ${error.message}`));
  };
  const openAsset = (banner: BannerCandidate) => {
    if (!activeJobId) return;
    openProtected(ApiClient.getBannerAssetBlob(activeJobId, banner.banner_id));
  };
  const bannerHistoryEntries: StandardHistoryEntry[] = history.map(item => ({
    id: item.run_id,
    title: formatHistoryDate(item.approved_at),
    subtitle: `${formatCountLabel(item.banner_count, 'banner', 'banners')} · ${formatCountLabel(item.brand_count, 'marca', 'marcas')}`,
    kindLabel: 'Banners',
    status: 'COMPLETED',
    onOpen: () => void reopenHistory(item.run_id),
    onDelete: () => requestDeleteHistory(item.run_id),
    deleteLabel: 'Excluir extração do histórico',
  }));

  return (
    <div className="page-content banners-page">
      <HistoryToggleButton
        title="Ver histórico de banners"
        count={history.length}
        open={!historyCollapsed}
        onClick={() => setHistoryCollapsed(value => !value)}
      />
      {!historyCollapsed && (
        <section className="history-panel banner-history-panel">
          <HistoryPanelBody
            items={bannerHistoryEntries}
            loading={historyLoading}
            emptyTitle="Nenhuma extração aprovada ainda"
            emptyDescription="As extrações concluídas e aprovadas aparecerão aqui automaticamente."
            onDeleteAll={handleDeleteAllHistory}
            deleteAllLabel="Apagar todos"
          />
        </section>
      )}
      <Panel className="glass-card" title="Extração de banners" subtitle="Selecione as marcas e extraia todos os banners desktop do carrossel principal.">
        <div className="brand-filter-panel">
          <div className="brand-filter-header">
            <div>
              <h3 className="brand-filter-title">Marcas ativas</h3>
              <p className="brand-filter-caption">{selectedBrands.length} de {activeBrands.length} marcas selecionadas</p>
            </div>
            <div className="brand-filter-actions">
              <Button variant="outline" disabled={busy} onClick={() => setSelectedBrands(activeBrands.map(brand => brand.brand_key))}>Selecionar todas</Button>
              <Button variant="outline" disabled={busy} onClick={() => setSelectedBrands([])}>Desmarcar todas</Button>
            </div>
          </div>
          {activeBrands.length === 0 ? (
            <EmptyState title="Nenhuma marca ativa disponível" description="Ative uma marca para iniciar uma extração." icon={<Images size={20} aria-hidden="true" />} />
          ) : (
            <div className="search-brand-grid brand-selector-grid banner-brand-grid">
              {activeBrands.map(brand => (
                <button type="button" key={brand.brand_key} disabled={busy}
                  className={`brand-chip ${selectedBrands.includes(brand.brand_key) ? 'active' : ''}`}
                  aria-pressed={selectedBrands.includes(brand.brand_key)} onClick={() => toggleBrand(brand.brand_key)}>
                  <div className="brand-chip-icon"><img src={brand.logo_url || `https://www.google.com/s2/favicons?domain=${brand.domain}&sz=64`} alt="" /></div>
                  <span>{brand.brand_name}</span>
                </button>
              ))}
            </div>
          )}
          <div className="banner-primary-action">
            {running ? (
              <Button fullWidth variant="error" onClick={() => void stop()} icon={<Square size={17} fill="currentColor" aria-hidden="true" />}>Parar extração</Button>
            ) : starting ? (
              <Button fullWidth loading loadingLabel="Iniciando extração" disabled>Extrair banners</Button>
            ) : (
              <Button fullWidth disabled={!selectedBrands.length} onClick={() => void start()} icon={<Images size={18} aria-hidden="true" />}>Extrair banners</Button>
            )}
          </div>
        </div>
      </Panel>

      {run && (
        <Panel className="glass-card" title={running ? 'Extração em andamento' : 'Resumo da extração'}>
          <div className="banner-progress-heading"><span>{processed} de {progressRows.length} marcas processadas</span><strong>{percent}%</strong></div>
          <div className="progress-bar-large" role="progressbar" aria-label="Progresso da extração" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
            <div className="progress-fill-large" style={{ width: `${percent}%` }} />
          </div>
          <div className="banner-progress-list">
            {progressRows.map(item => (
              <div className="banner-progress-row" key={item.brand_key}>
                <span>{item.brand_name}</span>
                <span className={`banner-status status-${item.status.toLowerCase()}`}>{statusLabel[item.status] || item.status}</span>
                <span>{item.banner_count} imagem(ns){item.video_count ? ` · ${item.video_count} vídeo(s)` : ''}</span>
                {item.status === 'WAITING_BROWSER' && <button type="button" className="btn-link" onClick={() => void retryAssisted()}>Tentar novamente</button>}
                {item.error && <small title={item.error}>{item.error}</small>}
              </div>
            ))}
          </div>
          {run.status === 'CANCELLED' && <Status tone="info" icon={<Pause size={18} aria-hidden="true" />}>Extração interrompida. Resultados parciais não serão salvos no histórico.</Status>}
          {run.status === 'PARTIAL' && <Status tone="warning" icon={<AlertTriangle size={18} aria-hidden="true" />}>Algumas marcas falharam. Os resultados ficam apenas nesta sessão e não entram no histórico.</Status>}
          {run.status === 'FAILED' && <Status tone="error" icon={<XCircle size={18} aria-hidden="true" />}>{run.error || 'Não foi possível concluir a extração.'}</Status>}
        </Panel>
      )}

      {run && (run.banners.length > 0 || videoSlides.length > 0) && activeJobId && (
        <Panel className="glass-card" title={reviewable ? 'Revisar banners e vídeos' : run.status === 'COMPLETED' ? 'Banners aprovados' : 'Resultados desta sessão'}>
          <div className="banner-review-toolbar">
            <div className="banner-review-counts">
              {run.banners.length > 0 && <strong>{selectedBannerIds.length} de {run.banners.length} imagens selecionadas</strong>}
              {videoSlides.length > 0 && <strong>{videoSlides.length} vídeo(s) detectado(s)</strong>}
            </div>
            <div className="banner-review-actions">
              {reviewable && run.banners.length > 0 && <>
                <Button variant="outline" size="sm" onClick={selectAllBanners}>Selecionar todos</Button>
                <Button variant="outline" size="sm" onClick={clearBanners}>Desmarcar todos</Button>
                <Button size="sm" disabled={!selectedBannerIds.length} onClick={handleApprove}>Aprovar {selectedBannerIds.length} banners</Button>
              </>}
              {reviewable && run.banners.length === 0 && videoSlides.length > 0 && <>
                <Button size="sm" onClick={handleApprove}>Concluir revisão ({videoSlides.length} vídeos)</Button>
              </>}
              {run.status === 'COMPLETED' && (
                <div className="banner-report-actions">
                  {(['json', 'csv', 'html'] as const).map(format => <Button variant="outline" size="sm" key={format} onClick={() => openProtected(ApiClient.getBannerReportBlob(run.run_id, format))}>{format.toUpperCase()}</Button>)}
                </div>
              )}
            </div>
          </div>
          {reviewable && run.banners.length > 0 && !selectedBannerIds.length && <p className="banner-selection-warning">Selecione ao menos um banner para aprovar.</p>}
          <div className="banner-gallery">
            {run.banners.map(banner => {
              const selected = selectedBannerIds.includes(banner.banner_id);
              return (
                <article key={banner.banner_id} className={`banner-card ${selected ? 'selected' : 'unselected'} ${reviewable ? 'reviewable' : ''}`}
                  role={reviewable ? 'checkbox' : undefined} aria-checked={reviewable ? selected : undefined} tabIndex={reviewable ? 0 : undefined}
                  onClick={() => reviewable && toggleBanner(banner.banner_id)}
                  onKeyDown={event => { if (reviewable && (event.key === ' ' || event.key === 'Enter')) { event.preventDefault(); toggleBanner(banner.banner_id); } }}>
                  {reviewable && <span className="banner-card-check">{selected && <Check size={16} />}</span>}
                  <div className="banner-preview"><ProtectedBannerImage runId={run.run_id} banner={banner} /></div>
                  <div className="banner-card-body">
                    <strong>{banner.brand_name} · Slide {banner.slide_order}</strong>
                    <button type="button" className="btn-link" onClick={event => { event.stopPropagation(); openAsset(banner); }}><ExternalLink size={14} /> Abrir imagem</button>
                  </div>
                </article>
              );
            })}
            {videoSlides.map(video => {
              const brandName = activeBrands.find(brand => brand.brand_key === video.brand_key)?.brand_name || video.brand_key;
              return (
                <article key={`${video.brand_key}-${video.slide_order}-${video.source_url || 'video'}`} className="banner-card banner-video-card">
                  <div className="banner-preview"><BannerVideoPreview video={video} brandName={brandName} /></div>
                  <div className="banner-card-body">
                    <strong>{brandName} · Vídeo {video.slide_order}</strong>
                    {video.source_url && <a className="btn-link" href={video.source_url} target="_blank" rel="noreferrer"><ExternalLink size={14} /> Abrir vídeo</a>}
                  </div>
                </article>
              );
            })}
          </div>
        </Panel>
      )}

      <ConfirmDialog state={confirmState} onClose={() => setConfirmState(null)} cancelLabel="Voltar à revisão" />
    </div>
  );
};
