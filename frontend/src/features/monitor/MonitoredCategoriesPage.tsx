// @ts-nocheck
import React, { useState, useEffect, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { AnimatePresence } from 'framer-motion';
import { 
  Plus, ExternalLink, Pause, Play, RefreshCw, 
  Trash2, AlertTriangle, Eye, MessageSquare, TrendingUp, Package, Clock, CheckCircle2 
} from 'lucide-react';
import { ApiClient, type CategoryPriceHistoryPoint } from '../../api/client';
import { deriveCategoryLifecycle } from '../../api/client';
import { useMonitorJobPolling } from '../../monitoring/useMonitorJobPolling';
import { ConfirmDialog, type ConfirmState } from '../../shared/ui/ConfirmDialog';
import { ProductDetailModal } from '../product/ProductDetailModal';
import { GlassCard } from '../../shared/ui/GlassCard';
import { LifecycleBadge } from '../../shared/ui/LifecycleBadge';
import { PriceChart } from '../../shared/ui/PriceChart';
import { EmptyState } from '../../components/ui/Feedback';
import { SkeletonRow, SkeletonChart } from '../../components/ui/Skeleton';
import { TableRegion } from '../../components/ui/TableRegion';
import { Dialog } from '../../components/ui/Dialog';
import { 
  formatTimeGlobal, AUTO_SWEEP_MAX_ATTEMPTS, AUTO_SWEEP_POLL_MS, getProductPriceView,
  isMarketplaceBrand,
} from '../../shared/lib';
import { reviewsStateLabel, productReviewComments, reviewCommentDisplayText } from '../../shared/lib/review';
import { ROUTES } from '../../routing/paths';

export const MonitoredCategoriesPage = ({ brands }: { brands: any[] }) => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const eligibleBrands = brands.filter(b => !isMarketplaceBrand(b));
  const [categories, setCategories] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [newCategory, setNewCategory] = useState({ url: '', brand: eligibleBrands.length > 0 ? eligibleBrands[0].brand_key : '' });
  const [submitting, setSubmitting] = useState(false);
  const [brandCategories, setBrandCategories] = useState<any[]>([]);
  const [loadingCategories, setLoadingCategories] = useState(false);
  const [manualMode, setManualMode] = useState(false);

  // Products modal
  const [selectedMonitor, setSelectedMonitor] = useState<any | null>(null);
  const [monitorProducts, setMonitorProducts] = useState<any[]>([]);
  const [loadingProducts, setLoadingProducts] = useState(false);
  const [selectedMonitorStockSummary, setSelectedMonitorStockSummary] = useState<any | null>(null);
  const [reviewLoadingIds, setReviewLoadingIds] = useState<Set<string>>(new Set());
  const [expandedProductId, setExpandedProductId] = useState<string | null>(null);
  const [priceHistoryById, setPriceHistoryById] = useState<Record<string, CategoryPriceHistoryPoint[]>>({});
  const [priceHistoryLoadingIds, setPriceHistoryLoadingIds] = useState<Set<string>>(new Set());
  const [autoSweepIds, setAutoSweepIds] = useState<Set<string>>(new Set());
  const autoSweepPollsRef = useRef<Record<string, number>>({});
  const categoryNotificationTargetRef = useRef<string | null>(null);
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);
  // --- Modal de detalhes de produto (Quick task 260712-iiz) ---
  const [detailModal, setDetailModal] = useState<{ brand: string; url: string; seed?: any } | null>(null);
  const [addingMonitorUrls, setAddingMonitorUrls] = useState<Set<string>>(new Set());

  const refreshCategoriesList = async () => {
    const data = await ApiClient.getMonitoredCategories();
    setCategories(data);
    return data;
  };

  const fetchCategories = async () => {
    setLoading(true);
    try {
      await refreshCategoriesList();
    } catch (err: any) {
      toast.error('Erro ao buscar categorias monitoradas: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    ApiClient.getMonitoredCategories()
      .then(data => {
        if (active) setCategories(data);
      })
      .catch((err: Error) => {
        if (active) toast.error('Erro ao buscar categorias monitoradas: ' + err.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    return () => {
      Object.values(autoSweepPollsRef.current).forEach(intervalId => window.clearInterval(intervalId));
      autoSweepPollsRef.current = {};
    };
  }, []);

  // Polling contínuo dos scans de cada monitor de categoria visível (Plan
  // 07-14, JOB-01): substitui o carregamento one-shot por conjunto/mount por
  // um map por monitor id que se reconecta sozinho, alimentando
  // deriveCategoryLifecycle (MON-01). Não interfere no auto-sweep de criação
  // (fluxo de produtos abaixo).
  const categoryMonitorIds = categories.map((item: any) => item.id).filter(Boolean);
  const { categoryScansByMonitorId } = useMonitorJobPolling({
    priceMonitorIds: [],
    categoryMonitorIds,
    enabled: true,
  });

  useEffect(() => {
    if (!newCategory.brand || !isModalOpen) return;
    let isMounted = true;

    const fetchTree = async () => {
      setLoadingCategories(true);
      try {
        const res = await ApiClient.request<any>(`/brands/${newCategory.brand}/discover`);
        if (isMounted) {
          const cats = res || [];
          setBrandCategories(cats);
          if (cats.length > 0 && !manualMode) {
            // The API returns { name: string, path: string } (where path is the URL)
            setNewCategory(prev => ({ ...prev, url: cats[0].path || cats[0].url }));
          }
        }
      } catch (e) {
        console.error("Failed to load categories", e);
        if (isMounted) {
          setBrandCategories([]);
          setManualMode(true); // Fallback to manual if failed
        }
      } finally {
        if (isMounted) setLoadingCategories(false);
      }
    };

    fetchTree();
    return () => { isMounted = false; };
  }, [newCategory.brand, isModalOpen, manualMode]);

  const handleViewProducts = async (monitor: any) => {
    setSelectedMonitor(monitor);
    setLoadingProducts(true);
    setSelectedMonitorStockSummary(null);
    try {
      const prods = await ApiClient.getMonitoredCategoryProducts(monitor.id);
      setMonitorProducts(prods);
      try {
        const summary = await Promise.resolve(null as any);
        setSelectedMonitorStockSummary(summary);
      } catch (summaryErr: any) {
        setSelectedMonitorStockSummary(null);
        const message = summaryErr?.message || '';
        if (!message.includes('404') && !message.includes('Resumo de estoque')) {
          toast.error('Erro ao buscar resumo de estoque');
        }
      }
    } catch (err: any) {
      toast.error('Erro ao buscar produtos: ' + err.message);
      setMonitorProducts([]);
      setSelectedMonitorStockSummary(null);
    } finally {
      setLoadingProducts(false);
    }
  };

  // A category notification carries the stable product URL rather than a
  // per-scan snapshot ID. This still finds the product after a later scan has
  // replaced the current snapshot, and the URL is only used for local lookup.
  useEffect(() => {
    const monitorId = searchParams.get('categoryMonitorId');
    const productUrl = searchParams.get('categoryProductUrl');
    if (!monitorId || !productUrl) return;

    const targetKey = `${monitorId}:${productUrl}`;
    if (categoryNotificationTargetRef.current === targetKey) return;
    const monitor = categories.find(category => String(category.id) === monitorId);
    if (!monitor) return;

    categoryNotificationTargetRef.current = targetKey;
    let cancelled = false;

    const openNotificationProduct = async () => {
      setSelectedMonitor(monitor);
      setLoadingProducts(true);
      setSelectedMonitorStockSummary(null);
      try {
        const products = await ApiClient.getMonitoredCategoryProducts(monitor.id);
        if (cancelled) return;
        setMonitorProducts(products);
        const product = products.find((candidate: any) => (
          [candidate.url, candidate.product_url, candidate.normalized_url].includes(productUrl)
        ));
        if (!product) return;

        const resolvedProductUrl = product.url || product.product_url || product.normalized_url || productUrl;
        setDetailModal({ brand: monitor.brand, url: resolvedProductUrl, seed: { ...product, url: resolvedProductUrl } });
        const nextParams = new URLSearchParams(searchParams);
        nextParams.delete('categoryMonitorId');
        nextParams.delete('categoryProductUrl');
        const query = nextParams.toString();
        navigate(`${ROUTES.monitor}${query ? `?${query}` : ''}`, { replace: true });
      } catch (err: any) {
        if (!cancelled) toast.error('Erro ao abrir o produto da notificação: ' + err.message);
      } finally {
        if (!cancelled) setLoadingProducts(false);
      }
    };

    void openNotificationProduct();
    return () => { cancelled = true; };
  }, [categories, navigate, searchParams]);

  const clearAutoSweepPoll = (monitorId: string) => {
    const intervalId = autoSweepPollsRef.current[monitorId];
    if (intervalId) {
      window.clearInterval(intervalId);
      delete autoSweepPollsRef.current[monitorId];
    }
    setAutoSweepIds(prev => {
      if (!prev.has(monitorId)) return prev;
      const next = new Set(prev);
      next.delete(monitorId);
      return next;
    });
  };

  const startAutoSweepPoll = (monitorId: string) => {
    clearAutoSweepPoll(monitorId);
    setAutoSweepIds(prev => {
      const next = new Set(prev);
      next.add(monitorId);
      return next;
    });

    let attempts = 0;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      attempts += 1;

      try {
        const latest = await ApiClient.getMonitoredCategories();
        setCategories(latest);
        const completed = latest.find((item: any) => item.id === monitorId && item.last_scraped_at);

        if (completed) {
          clearAutoSweepPoll(monitorId);
          await handleViewProducts(completed);
          return;
        }

        if (attempts >= AUTO_SWEEP_MAX_ATTEMPTS) {
          clearAutoSweepPoll(monitorId);
        }
      } catch {
        clearAutoSweepPoll(monitorId);
        toast.error('Categoria salva, mas a primeira varredura falhou. Tente novamente na lista.');
      } finally {
        inFlight = false;
      }
    };

    autoSweepPollsRef.current[monitorId] = window.setInterval(poll, AUTO_SWEEP_POLL_MS);
    void poll();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      let finalUrl = newCategory.url.trim();
      const selectedBrand = brands.find(b => b.brand_key === newCategory.brand);
      if (selectedBrand && selectedBrand.domain && !finalUrl.startsWith('http')) {
        const domain = selectedBrand.domain.replace(/^https?:\/\//, '');
        finalUrl = `https://${domain}${finalUrl.startsWith('/') ? '' : '/'}${finalUrl}`;
      }

      const created = await ApiClient.createMonitoredCategory({ ...newCategory, url: finalUrl }) as any;
      setNewCategory({ url: '', brand: brands.length > 0 ? brands[0].brand_key : '' });
      setIsModalOpen(false);
      toast.success('Categoria adicionada. Iniciando primeira varredura…');

      if (created?.id) {
        setCategories(prev => (
          prev.some(item => item.id === created.id)
            ? prev
            : [...prev, created]
        ));
        startAutoSweepPoll(created.id);
      } else {
        await fetchCategories();
        toast.error('Categoria salva, mas a primeira varredura falhou. Tente novamente na lista.');
      }
    } catch (err: any) {
      toast.error("Erro ao adicionar: " + err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = (monitorId: string) => {
    setConfirmState({
      message: 'Excluir monitor: Deseja excluir este monitor? Esta ação é permanente.',
      action: async () => {
        clearAutoSweepPoll(monitorId);
        try {
          await ApiClient.deleteMonitoredCategory(monitorId);
          fetchCategories();
        } catch (err: any) {
          toast.error('Erro ao excluir: ' + err.message);
        }
      },
    });
  };

  const handlePauseCategory = async (monitorId: string) => {
    try {
      await ApiClient.pauseCategoryMonitor(monitorId);
      toast.success('Monitor pausado com sucesso.');
      fetchCategories();
    } catch (err: any) {
      toast.error('Erro ao pausar monitor: ' + err.message);
    }
  };

  const handleResumeCategory = async (monitorId: string) => {
    try {
      await ApiClient.resumeCategoryMonitor(monitorId);
      toast.success('Monitor retomado com sucesso.');
      fetchCategories();
    } catch (err: any) {
      toast.error('Erro ao retomar monitor: ' + err.message);
    }
  };

  const mergeMonitorProductResult = (scanProductId: string, fields: any) => {
    setMonitorProducts(prev => prev.map(product => (
      product.scan_product_id === scanProductId
        ? { ...product, ...fields }
        : product
    )));
  };


  // Historico de preco de um produto de categoria e sob demanda (MON-02): so
  // chama getMonitoredProductPriceHistory no toggle de abertura, nunca em mount.
  const handleTogglePriceHistory = async (product: any) => {
    const scanProductId = product?.scan_product_id;
    if (!selectedMonitor?.id || !scanProductId) return;
    if (expandedProductId === scanProductId) {
      setExpandedProductId(null);
      return;
    }
    setExpandedProductId(scanProductId);
    if (priceHistoryById[scanProductId]) return;
    setPriceHistoryLoadingIds(prev => new Set(prev).add(scanProductId));
    try {
      const result = await ApiClient.getMonitoredProductPriceHistory(selectedMonitor.id, scanProductId);
      setPriceHistoryById(prev => ({ ...prev, [scanProductId]: result.points }));
    } catch (err: any) {
      toast.error(err.message || 'Erro ao buscar histórico de preços');
    } finally {
      setPriceHistoryLoadingIds(prev => {
        const next = new Set(prev);
        next.delete(scanProductId);
        return next;
      });
    }
  };

  const handleRequestReviewComments = async (product: any) => {
    const scanProductId = product?.scan_product_id;
    if (!selectedMonitor?.id || !scanProductId) return;
    setReviewLoadingIds(prev => new Set(prev).add(scanProductId));
    try {
      const result = await ApiClient.requestMonitoredProductReviews(selectedMonitor.id, scanProductId);
      mergeMonitorProductResult(scanProductId, result);
      if (result.reviews_state === 'available' && result.comments.length > 0) {
        toast.success('Comentarios de avaliacao atualizados');
      } else {
        toast.info(`Avaliacoes: ${reviewsStateLabel(result.reviews_state)}`);
      }
    } catch (err: any) {
      toast.error(err.message || 'Erro ao buscar comentários de avaliação');
    } finally {
      setReviewLoadingIds(prev => {
        const next = new Set(prev);
        next.delete(scanProductId);
        return next;
      });
    }
  };

  const handleAddToMonitor = async (url: string, brand: string) => {
    setAddingMonitorUrls(prev => new Set(prev).add(url));
    try {
      const result = await ApiClient.addToMonitor(url, brand);
      if (result.status === 'already_active') {
        toast.info('Produto já está em monitoramento de produto único');
      } else if (result.status === 'reactivated') {
        toast.success('Monitor de produto único reativado com sucesso');
      } else {
        toast.success('Produto enviado para o monitor de produto único!');
      }
    } catch (err: any) {
      toast.error(err.message || 'Erro ao enviar para monitor de produto único');
    } finally {
      setAddingMonitorUrls(prev => {
        const next = new Set(prev);
        next.delete(url);
        return next;
      });
    }
  };

  return (
    <div className="page-content priority-page monitored-categories-page">
      <GlassCard
        title="Categorias monitoradas"
        subtitle="Consolide varreduras, estoque, ruptura e avaliações por categoria."
        action={
          <button className="btn btn-primary" onClick={() => {
            const firstEligible = eligibleBrands[0]?.brand_key || '';
            setNewCategory(prev => (prev.brand && !isMarketplaceBrand(prev.brand))
              ? prev
              : { ...prev, brand: firstEligible });
            setIsModalOpen(true);
          }}>
            <Plus size={18} aria-hidden="true" /> Novo monitor
          </button>
        }
      >
        {loading ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', padding: '16px' }}>
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : categories.length === 0 ? (
          <EmptyState title="Nenhuma categoria monitorada" description="Crie um monitor para iniciar a primeira varredura da categoria." />
        ) : (
          <TableRegion label="Monitoramentos de categoria">
            <table className="modern-table">
              <thead>
                <tr>
                  <th>Marca</th>
                  <th>URL</th>
                  <th>Status</th>
                  <th>Execução</th>
                  <th>Última tentativa / Último sucesso</th>
                  <th>Ações</th>
                </tr>
              </thead>
              <tbody>
                {categories.map((c, i) => {
                  const isAutoSweepPending = autoSweepIds.has(c.id) && !c.last_scraped_at;
                  const categoryScans = categoryScansByMonitorId[c.id] ?? [];
                  const lifecycle = deriveCategoryLifecycle(c, categoryScans);
                  return (
                    <tr key={c.id || i}>
                      <td>
                        <span className="category-brand-badge">
                          {brands.find(b => b.brand_key === c.brand)?.brand_name || c.brand}
                        </span>
                      </td>
                      <td className="category-url-cell">
                        <div className="category-url-link">
                          <a href={c.url} target="_blank" rel="noopener noreferrer">{c.url}</a>
                          <a href={c.url} target="_blank" rel="noopener noreferrer" className="category-url-external" title="Abrir no navegador" aria-label="Abrir categoria no navegador">
                            <ExternalLink size={14} />
                          </a>
                        </div>
                      </td>
                      <td>
                        {c.status === 'active' ? (
                          <span className="category-status text-success"><CheckCircle2 size={14} aria-hidden="true" /> Ativo</span>
                        ) : c.status === 'paused' ? (
                          <span className="category-status text-warning"><Pause size={14} aria-hidden="true" /> Pausado</span>
                        ) : (
                          <span className="text-error">Inativo</span>
                        )}
                        {c.last_map_violation_count > 0 && (
                          <span className="category-status category-status--error">
                            <AlertTriangle size={14} aria-hidden="true" /> MAP: {c.last_map_violation_count}
                          </span>
                        )}
                      </td>
                      <td>
                        <LifecycleBadge state={lifecycle.state} />
                      </td>
                      <td className="text-muted">
                        {isAutoSweepPending ? (
                          <span className="category-status">
                            <RefreshCw className="animate-spin" size={14} />
                            Varredura...
                          </span>
                        ) : lifecycle.lastAttemptAt || lifecycle.lastSuccessAt ? (
                          <>
                            {lifecycle.lastAttemptAt && <div>Última tentativa: {formatTimeGlobal(lifecycle.lastAttemptAt)}</div>}
                            {lifecycle.lastSuccessAt && <div>Último sucesso: {formatTimeGlobal(lifecycle.lastSuccessAt)}</div>}
                            {lifecycle.nextRetryAt && <div>Próxima tentativa: {formatTimeGlobal(lifecycle.nextRetryAt)}</div>}
                          </>
                        ) : (
                          'Nunca'
                        )}
                      </td>
                      <td>
                        <div className="table-row-actions">
                          <button className="btn btn-icon btn-outline" onClick={() => handleViewProducts(c)} title="Ver Produtos" aria-label="Ver produtos monitorados">
                            <Eye size={16} />
                          </button>
                          {c.status === 'paused' ? (
                            <button className="btn btn-icon btn-outline text-success" onClick={() => handleResumeCategory(c.id)} title="Retomar Monitor" aria-label="Retomar monitor de categoria">
                              <Play size={16} />
                            </button>
                          ) : (
                            <button className="btn btn-icon btn-outline text-warning" onClick={() => handlePauseCategory(c.id)} title="Pausar Monitor" aria-label="Pausar monitor de categoria">
                              <Pause size={16} />
                            </button>
                          )}
                          <button className="btn btn-icon btn-outline text-error" onClick={() => handleDelete(c.id)} title="Excluir Monitor" aria-label="Excluir monitor de categoria">
                            <Trash2 size={16} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableRegion>
        )}
      </GlassCard>

      {isModalOpen && (
        <Dialog
          title="Adicionar Categoria"
          onClose={() => setIsModalOpen(false)}
          busy={submitting}
          closeOnEscape={!submitting}
          closeOnOverlay={!submitting}
        >
          <form onSubmit={handleSubmit} className="form-stack">
            <div className="form-group">
              <label className="label">Marca</label>
              <select
                className="input"
                value={newCategory.brand}
                onChange={e => setNewCategory({ ...newCategory, brand: e.target.value })}
              >
                {eligibleBrands.map(b => (
                  <option key={b.brand_key} value={b.brand_key}>{b.brand_name}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <div className="category-field-heading">
                <label className="label">Categoria</label>
                <button
                  type="button"
                  onClick={() => setManualMode(!manualMode)}
                  className="category-mode-toggle"
                >
                  {manualMode ? 'Selecionar da Lista' : 'Inserir Manualmente'}
                </button>
              </div>

              {manualMode ? (
                <input
                  type="url"
                  className="input"
                  placeholder="https://..."
                  value={newCategory.url}
                  onChange={e => setNewCategory({ ...newCategory, url: e.target.value })}
                  required
                />
              ) : (
                loadingCategories ? (
                  <div className="category-tree-state category-tree-state--loading">
                    <RefreshCw className="animate-spin" size={14} /> Carregando árvore de categorias...
                  </div>
                ) : brandCategories.length === 0 ? (
                  <div className="category-tree-state category-tree-state--warning">
                    Nenhuma categoria mapeada. <button type="button" className="category-inline-action" onClick={() => setManualMode(true)}>Insira manualmente.</button>
                  </div>
                ) : (
                  <select
                    className="input"
                    value={newCategory.url}
                    onChange={e => setNewCategory({ ...newCategory, url: e.target.value })}
                    required
                  >
                    {brandCategories.map((c: any, i: number) => (
                      <option key={i} value={c.path || c.url}>{c.name}</option>
                    ))}
                  </select>
                )
              )}
            </div>
            <div className="dialog-actions dialog-actions--equal">
              <button type="button" className="btn btn-outline" onClick={() => setIsModalOpen(false)}>Cancelar</button>
              <button type="submit" className="btn btn-primary" disabled={submitting}>
                {submitting ? <RefreshCw className="animate-spin" size={18} /> : 'Salvar'}
              </button>
            </div>
          </form>
        </Dialog>
      )}

      {selectedMonitor && (
        <Dialog title="Produtos em Monitoramento" onClose={() => setSelectedMonitor(null)} size="lg">
          <p className="text-muted monitored-products-context">
            <span className="badge monitored-products-brand">{selectedMonitor.brand}</span>
            {selectedMonitor.url}
          </p>
          {selectedMonitorStockSummary && (
            <div className="stock-summary-grid">
              <div className="badge stock-summary-card stock-summary-card--success">
                <span>Verificados:</span>
                <strong>{selectedMonitorStockSummary.verified_stock_count}</strong>
              </div>
              <div className="badge stock-summary-card stock-summary-card--neutral">
                <span>Nao verificados:</span>
                <strong>{selectedMonitorStockSummary.unknown_stock_count}</strong>
              </div>
              <div className="badge stock-summary-card stock-summary-card--error">
                <span>Esgotados:</span>
                <strong>{selectedMonitorStockSummary.out_of_stock_count}</strong>
              </div>
              {selectedMonitorStockSummary.rupture_pct !== null && (
                <div className="badge stock-summary-card stock-summary-card--warning">
                  <span>Ruptura:</span>
                  <strong>{Math.round(selectedMonitorStockSummary.rupture_pct * 100)}%</strong>
                </div>
              )}
            </div>
          )}

          {loadingProducts ? (
            <div className="monitored-products-loading"><RefreshCw className="animate-spin" /></div>
          ) : monitorProducts.length === 0 ? (
            <div className="empty-state monitored-products-empty">
              Nenhum produto extraído ainda. Aguarde a próxima varredura.
            </div>
          ) : (
            <div className="monitored-products-grid">
              {monitorProducts.map((p, i) => {
                const productUrl = p.url || p.product_url || p.normalized_url || '';
                return (
                <div
                  key={p.scan_product_id || p.id || i}
                  className="product-card"
                  role={productUrl ? 'button' : undefined}
                  tabIndex={productUrl ? 0 : undefined}
                  style={productUrl ? { cursor: 'pointer' } : undefined}
                  onClick={() => productUrl && setDetailModal({ brand: selectedMonitor.brand, url: productUrl, seed: { ...p, url: productUrl } })}
                  onKeyDown={productUrl ? (event) => {
                    if (event.target !== event.currentTarget) return;
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setDetailModal({ brand: selectedMonitor.brand, url: productUrl, seed: { ...p, url: productUrl } });
                    }
                  } : undefined}
                >
                  <div className="product-image">
                    {p.image_url ? (
                      <img src={p.image_url} alt={p.raw_title || p.name} />
                    ) : (
                      <Package size={40} />
                    )}
                    {(() => {
                      const priceView = getProductPriceView(p);
                      return priceView.discountPct != null
                        ? <span className="badge-discount">-{priceView.discountPct}%</span>
                        : null;
                    })()}
                  </div>
                  <div className="product-details">
                    <div className="product-card-heading">
                      <p className="product-name" title={p.raw_title}>{p.raw_title || 'Produto sem título'}</p>
                      {productUrl && (
                        <a
                          href={productUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-muted product-external-icon"
                          title="Abrir no site"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <ExternalLink size={14} />
                        </a>
                      )}
                    </div>
                    <div className="product-price product-price-row">
                      {(() => {
                        const priceView = getProductPriceView(p);
                        return (
                          <>
                            {priceView.original != null ? (
                              <span className="price-original">
                                R$ {priceView.original.toFixed(2)}
                              </span>
                            ) : null}
                            <span className="price-current">R$ {(priceView.current ?? 0).toFixed(2)}</span>
                          </>
                        );
                      })()}
                    </div>

                    <div className="monitored-product-meta">
                      {p.stock_availability === true && <span className="text-success">Em estoque</span>}
                      {p.stock_availability === false && <span className="text-error">Esgotado</span>}
                      {p.stock_availability == null && <span>Estoque nao verificado</span>}
                      {p.reviews_state && (
                        <span className="monitored-product-rating">
                          {p.rating != null ? (
                            <strong className="text-amber-500 font-semibold">★ {Number(p.rating).toFixed(1)} </strong>
                          ) : null}
                          {p.review_count != null ? (
                            <span>({p.review_count} {p.review_count === 1 ? 'avaliação' : 'avaliações'})</span>
                          ) : (
                            <span>({reviewsStateLabel(p.reviews_state, p.review_count)})</span>
                          )}
                        </span>
                      )}
                      {productReviewComments(p).length > 0 && (
                        <div className="monitored-product-comments">
                          {productReviewComments(p).slice(0, 3).map((comment: any, idx: number) => (
                            <span key={comment.review_id || idx} className="monitored-comment-chip" title={reviewCommentDisplayText(comment)}>
                              {comment.rating ? <span className="text-amber-500 font-semibold">★ {comment.rating} </span> : null}
                              {reviewCommentDisplayText(comment)}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                  {(productUrl || p.scan_product_id) && (
                    <div className="monitored-product-actions">
                      {p.scan_product_id && (
                        <>
                          <button
                            type="button"
                            className="btn-icon btn-sm"
                            title="Buscar comentários de avaliação"
                            disabled={reviewLoadingIds.has(p.scan_product_id)}
                            onClick={async (e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              await handleRequestReviewComments(p);
                            }}
                          >
                            {reviewLoadingIds.has(p.scan_product_id) ? <RefreshCw className="animate-spin" size={14} /> : <MessageSquare size={14} />}
                          </button>
                          <button
                            type="button"
                            className="btn-icon btn-sm"
                            title="Ver histórico de preços"
                            aria-expanded={expandedProductId === p.scan_product_id}
                            onClick={async (e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              await handleTogglePriceHistory(p);
                            }}
                          >
                            <TrendingUp size={14} />
                          </button>
                        </>
                      )}
                      {productUrl && (
                        <button
                          type="button"
                          className="btn-icon btn-sm"
                          title="Enviar para monitor de produto único"
                          aria-label="Enviar para monitor de produto único"
                          disabled={addingMonitorUrls.has(productUrl)}
                          onClick={async (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            await handleAddToMonitor(productUrl, selectedMonitor.brand);
                          }}
                        >
                          {addingMonitorUrls.has(productUrl) ? (
                            <RefreshCw className="animate-spin" size={14} />
                          ) : (
                            <Plus size={14} />
                          )}
                        </button>
                      )}
                    </div>
                  )}
                  {p.scan_product_id && expandedProductId === p.scan_product_id && (
                    <div className="monitored-product-history" onClick={e => e.stopPropagation()}>
                      {priceHistoryLoadingIds.has(p.scan_product_id) ? (
                        <SkeletonChart />
                      ) : (
                        <PriceChart
                          history={priceHistoryById[p.scan_product_id] || []}
                          emptyTitle="Histórico ainda não disponível"
                          emptyDescription="Aguardando o próximo scan bem-sucedido desta categoria para gerar o gráfico."
                        />
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            </div>
          )}
        </Dialog>
      )}

      <ConfirmDialog state={confirmState} onClose={() => setConfirmState(null)} cancelLabel="Manter monitor" />
      <ProductDetailModal
        open={!!detailModal}
        brand={detailModal?.brand || ''}
        url={detailModal?.url || ''}
        seed={detailModal?.seed}
        onClose={() => setDetailModal(null)}
      />
    </div>
  );
};
