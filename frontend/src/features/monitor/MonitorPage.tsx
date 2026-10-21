import React, { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router';
import { Clock, Package, CheckCircle2, Trash2, RefreshCw, Pause, Play, TrendingUp, LayoutDashboard } from 'lucide-react';
import { toast } from 'sonner';
import { AnimatePresence } from 'framer-motion';
import { ApiClient } from '../../api/client';
import { ConfirmDialog, type ConfirmState } from '../../shared/ui/ConfirmDialog';
import { PriceChart } from '../../shared/ui/PriceChart';
import { StatusBanner } from '../../shared/ui/StatusBanner';
import { LifecycleBadge } from '../../shared/ui/LifecycleBadge';
import { Panel } from '../../components/ui/Panel';
import { EmptyState } from '../../components/ui/Feedback';
import { ProductDetailModal, MonitorConditionalOffers } from '../product/ProductDetailModal';
import {
  formatTimeGlobal,
  getMonitorPriceView,
  formatOfferPrice,
  offerStateMeta,
  domainMatchesBrand,
  brandDisplayName,
  type BrandItem,
} from '../../shared/lib';
import {
  deriveIndividualLifecycle,
  type PriceMonitorConfig,
} from '../../api/client';
import type { ProductDetailSeed } from '../product/ProductDetailModal';

export const MonitorPage = ({ brands }: { brands: BrandItem[] }) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [monitors, setMonitors] = useState<PriceMonitorConfig[]>([]);
  const [url, setUrl] = useState('');
  const [brand, setBrand] = useState('');
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<{ type: 'success' | 'error' | 'info', message: string } | null>(null);
  const [expandedMonitorId, setExpandedMonitorId] = useState<string | null>(null);
  const [identifiedBrandName, setIdentifiedBrandName] = useState<string | null>(null);
  const [showManualBrand, setShowManualBrand] = useState(false);
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);
  const [detailModal, setDetailModal] = useState<{ brand: string; url: string; seed?: ProductDetailSeed } | null>(null);

  const refreshMonitors = () => {
    ApiClient.getMonitors().then(data => {
      const list = Array.isArray(data) ? data : Object.values(data);
      setMonitors(list as PriceMonitorConfig[]);
    }).catch(() => setMonitors([]));
  };

  useEffect(() => {
    refreshMonitors();
    const intervalId = setInterval(refreshMonitors, 5000);
    return () => clearInterval(intervalId);
  }, []);

  useEffect(() => {
    const targetMonitorId = searchParams.get('monitorId');
    if (!targetMonitorId) return;

    const targetMonitor = monitors.find(monitor => monitor.job_id === targetMonitorId);
    if (!targetMonitor?.url) return;

    setDetailModal({ brand: targetMonitor.brand, url: targetMonitor.url, seed: targetMonitor });
    const nextSearchParams = new URLSearchParams(searchParams);
    nextSearchParams.delete('monitorId');
    setSearchParams(nextSearchParams, { replace: true });
  }, [monitors, searchParams, setSearchParams]);

  const handleDeleteMonitor = (jobId: string) => {
    setConfirmState({
      message: 'Excluir monitor: Deseja excluir este monitor? Esta ação é permanente.',
      action: async () => {
        try {
          await ApiClient.deleteMonitor(jobId);
          refreshMonitors();
          setStatus({ type: 'success', message: 'Monitor excluído!' });
        } catch (err: unknown) {
          const message = err instanceof Error ? err.message : String(err);
          setStatus({ type: 'error', message: 'Erro ao excluir monitor: ' + message });
        }
      },
    });
  };

  const handleToggleActive = async (jobId: string, isActive: boolean) => {
    try {
      if (isActive) {
        await ApiClient.stopMonitor(jobId);
        setStatus({ type: 'success', message: 'Monitoramento desativado.' });
      } else {
        await ApiClient.resumeMonitor(jobId);
        setStatus({ type: 'success', message: 'Monitoramento reativado.' });
      }
      refreshMonitors();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setStatus({ type: 'error', message: 'Erro ao alterar status: ' + message });
    }
  };

  useEffect(() => {
    if (brands.length > 0 && !brand) {
      setTimeout(() => setBrand(brands[0].brand_key), 0);
    }
  }, [brands, brand]);

  const startMonitorForBrand = async (productUrl: string, brandKey: string, brandLabel?: string) => {
    const result = await ApiClient.addToMonitor(productUrl, brandKey);
    if (result.status === 'already_active') {
      setStatus({ type: 'info', message: 'Produto já está em monitoramento' });
      toast.info('Produto já está em monitoramento');
    } else if (result.status === 'reactivated') {
      setStatus({ type: 'success', message: 'Monitor reativado' });
      toast.success('Monitor reativado');
    } else {
      setStatus({ type: 'success', message: brandLabel ? `Adicionado ao monitoramento (${brandLabel})` : 'Adicionado ao monitoramento' });
      toast.success('Adicionado ao monitoramento');
    }
    setUrl('');
    setShowManualBrand(false);
    setIdentifiedBrandName(null);
    refreshMonitors();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url) return;

    if (showManualBrand) {
      if (!brand) {
        setStatus({ type: 'error', message: 'Selecione uma marca para iniciar o monitoramento.' });
        return;
      }
      setLoading(true);
      setStatus({ type: 'info', message: 'Iniciando monitoramento...' });
      try {
        const meta = brands.find(b => b.brand_key === brand);
        await startMonitorForBrand(url, brand, meta?.brand_name);
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        setStatus({ type: 'error', message: 'Erro ao iniciar monitor: ' + message });
        toast.error(message || 'Erro ao adicionar ao monitoramento');
      } finally {
        setLoading(false);
      }
      return;
    }

    setLoading(true);
    setIdentifiedBrandName(null);
    setStatus({ type: 'info', message: 'Identificando marca pelo link...' });
    try {
      const identified = await ApiClient.identifyBrand(url);
      const matched = brands.find(b => domainMatchesBrand(identified.domain, b.domain));

      if (matched) {
        setIdentifiedBrandName(matched.brand_name);
        await startMonitorForBrand(url, matched.brand_key, matched.brand_name);
      } else {
        setIdentifiedBrandName(null);
        setShowManualBrand(true);
        setStatus({
          type: 'info',
          message: 'Não identificamos uma marca cadastrada para este domínio. Selecione a marca manualmente.',
        });
        toast.info('Selecione a marca manualmente para este produto.');
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setStatus({ type: 'error', message: 'Erro ao identificar marca: ' + message });
      toast.error(message || 'Erro ao identificar marca');
    } finally {
      setLoading(false);
    }
  };


  return (
    <div className="page-content monitor-page">
      <Panel className="glass-card" title="Novo monitoramento" subtitle="Cole a URL do produto; a marca é identificada antes de iniciar o acompanhamento.">
        {status && <StatusBanner type={status.type} message={status.message} onClear={() => setStatus(null)} />}
        <form className="monitor-form-row" onSubmit={handleSubmit}>
          <div className="monitor-url-field field">
            <label className="field__label" htmlFor="monitor-product-url">URL do produto</label>
            <input
              id="monitor-product-url"
              type="url"
              className="input"
              placeholder="Cole o link do produto — identificamos a marca automaticamente"
              value={url}
              onChange={e => {
                setUrl(e.target.value);
                setIdentifiedBrandName(null);
                setShowManualBrand(false);
              }}
              required
            />
          </div>
          {showManualBrand && (
            <div className="monitor-brand-field field">
              <label className="field__label" htmlFor="monitor-brand">Marca concorrente</label>
              <select
                id="monitor-brand"
                className="input"
                value={brand}
                onChange={e => setBrand(e.target.value)}
                required
              >
                <option value="">Selecione...</option>
                {brands && brands.map(b => <option key={b.brand_key} value={b.brand_key}>{b.brand_name}</option>)}
              </select>
            </div>
          )}
          <button type="submit" className="button button--primary monitor-submit-btn" disabled={loading || !url}>
            {loading ? <RefreshCw className="animate-spin" size={16} aria-hidden="true" /> : <LayoutDashboard size={16} aria-hidden="true" />}
            <span>{loading ? "Iniciando…" : "Iniciar monitoramento"}</span>
          </button>
        </form>
        {identifiedBrandName && (
          <p className="text-muted monitor-brand-identified">
            <CheckCircle2 size={14} className="text-success" /> Marca identificada: <strong>{identifiedBrandName}</strong>
          </p>
        )}
      </Panel>

      <Panel className="glass-card" title="Monitores ativos" subtitle="Abra um item para consultar todos os detalhes ou expanda seu histórico de preços.">
        <div className="monitor-list">
          {!monitors || monitors.length === 0 ? (
            <EmptyState
              title="Nenhum monitor ativo"
              description="Cole a URL de um produto acima para iniciar o primeiro monitoramento."
              icon={<Clock size={24} aria-hidden="true" />}
            />
          ) : (
            monitors.map((m) => (
              <div
                key={m.job_id}
                className={`monitor-item ${m.url ? 'is-interactive' : ''}`}
                role={m.url ? 'button' : undefined}
                tabIndex={m.url ? 0 : undefined}
                aria-label={m.url ? `Abrir detalhes de ${m.product_name || 'produto monitorado'}` : undefined}
                onClick={() => m.url && setDetailModal({ brand: m.brand, url: m.url, seed: m })}
                onKeyDown={m.url ? (event) => {
                  if (event.target !== event.currentTarget) return;
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setDetailModal({ brand: m.brand, url: m.url, seed: m });
                  }
                } : undefined}
              >
                {(() => {
                  const priceView = getMonitorPriceView(m);
                  const lifecycle = deriveIndividualLifecycle(m);
                  const productName = typeof m.product_name === 'string' ? m.product_name : undefined;
                  const imageUrl = typeof m.image_url === 'string' ? m.image_url : undefined;
                  return (
                    <>
                      <div className="monitor-image-small">
                        {imageUrl ? (
                          <img
                            src={imageUrl}
                            alt={productName || 'Produto'}
                            referrerPolicy="no-referrer"
                            onError={(e) => {
                              // If image fails to load, hide broken image and display fallback icon
                              e.currentTarget.style.display = 'none';
                              const parent = e.currentTarget.parentElement;
                              if (parent && !parent.querySelector('svg')) {
                                const placeholder = document.createElement('span');
                                placeholder.innerHTML = '📦';
                                placeholder.style.fontSize = '20px';
                                parent.appendChild(placeholder);
                              }
                            }}
                          />
                        ) : (
                          <Package size={24} className="text-muted" />
                        )}
                      </div>

                      <div className="monitor-info">
                        <div className="monitor-main">
                          <span className="brand-badge-small">{brandDisplayName(m.brand, brands)}</span>
                          <LifecycleBadge state={lifecycle.state} />
                        </div>
                        {productName && <p className="monitor-product-name">{productName}</p>}
                        <span className="monitor-url" title={m.url}>{m.url}</span>
                        {lifecycle.lastSuccessAt && (
                          <span className="monitor-last-reading">
                            <CheckCircle2 size={12} aria-hidden="true" /> Último sucesso: {formatTimeGlobal(lifecycle.lastSuccessAt)}
                          </span>
                        )}
                        <span className="monitor-last-reading">
                          <Clock size={12} aria-hidden="true" /> Próxima tentativa: {lifecycle.nextScheduledAttemptAt ? formatTimeGlobal(lifecycle.nextScheduledAttemptAt) : 'não agendada'}
                        </span>
                      </div>

                      <div className="monitor-pricing">
                        {priceView.current != null ? (
                          <>
                            {priceView.original != null ? (
                              <span className="price-original">R$ {priceView.original.toFixed(2)}</span>
                            ) : null}
                            <span className="monitor-price-value">R$ {priceView.current.toFixed(2)}</span>
                          </>
                        ) : (
                          <span className="monitor-price-pending">Pendente...</span>
                        )}
                        <span className={`status-pill ${m.active ? 'status-pill--active' : 'status-pill--inactive'}`}>
                          {m.active ? 'Ativo' : 'Inativo'}
                        </span>
                        {(m.conditional_offers_current || []).filter(offer => offer.state !== 'expired').map(offer => (
                          <div className="monitor-offer__card" key={offer.offer_key || offer.condition_label}>
                            <span>{offer.condition_label}</span>
                            <strong>{formatOfferPrice(offer) || 'Valor não disponível'}</strong>
                            <span>{offerStateMeta(offer.state).label}</span>
                          </div>
                        ))}
                        {(m.conditional_offers_status === 'blocked' || m.conditional_offers_status === 'error') && (
                          <span className="monitor-offer__diagnostic">
                            Última oferta mantida — {m.conditional_offers_status === 'blocked' ? 'verificação bloqueada' : 'erro na verificação'}
                          </span>
                        )}
                      </div>

                      <div className="monitor-actions">
                        <button
                          type="button"
                          className="icon-button icon-button--outline"
                          aria-label="Ver histórico de preços"
                          aria-expanded={expandedMonitorId === m.job_id}
                          onClick={(e) => { e.stopPropagation(); setExpandedMonitorId(expandedMonitorId === m.job_id ? null : m.job_id); }}
                          title="Ver histórico de preços"
                        >
                          <TrendingUp size={16} />
                        </button>
                        <button
                          type="button"
                          className="icon-button icon-button--outline"
                          aria-label={m.active ? "Pausar monitoramento" : "Retomar monitoramento"}
                          onClick={(e) => { e.stopPropagation(); handleToggleActive(m.job_id, Boolean(m.active)); }}
                          title={m.active ? "Pausar monitoramento" : "Retomar monitoramento"}
                        >
                          {m.active ? <Pause size={16} /> : <Play size={16} />}
                        </button>
                        <button
                          type="button"
                          className="icon-button icon-button--destructive"
                          aria-label="Excluir monitor"
                          onClick={(e) => { e.stopPropagation(); handleDeleteMonitor(m.job_id); }}
                          title="Excluir monitor"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </>
                  );
                })()}

                <AnimatePresence>
                  {expandedMonitorId === m.job_id && (
                    <div className="monitor-chart-row" onClick={e => e.stopPropagation()}>
                      <PriceChart history={m.history || []} />
                      <MonitorConditionalOffers monitor={m} />
                    </div>
                  )}
                </AnimatePresence>
              </div>
            ))
          )}
        </div>
      </Panel>

      <ConfirmDialog state={confirmState} onClose={() => setConfirmState(null)} cancelLabel="Manter monitor" />
      <ProductDetailModal
        open={!!detailModal}
        brand={detailModal?.brand || ''}
        url={detailModal?.url || ''}
        seed={detailModal?.seed}
        showMonitorAction={false}
        onClose={() => setDetailModal(null)}
      />
    </div>
  );
};
