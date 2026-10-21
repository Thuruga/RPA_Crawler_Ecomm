/* eslint-disable @typescript-eslint/no-explicit-any */
import React, { useState, useEffect, useRef } from 'react';
import {
  Search,
  Package,
  CheckCircle2,
  Plus,
  RefreshCw,
  ExternalLink,
  FileSpreadsheet,
  Radar,
  AlertTriangle,
  Check,
  MapPin,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  SKU_PATTERN,
  SKU_ERROR_MSG,
} from '../../shared/lib';
import { JobProgressTerminal } from '../../shared/ui/JobProgressTerminal';
import { CrossResultsSkeleton } from '../search/Skeletons';
import { HistoryList, HistoryToggleButton } from '../history/HistoryList';
import { GlassCard } from '../../shared/ui/GlassCard';
import { ProductDetailModal } from '../product/ProductDetailModal';
import { ApiClient } from '../../api/client';
import { useSearchStore } from '../../stores/searchStore';
import { useShallow } from 'zustand/react/shallow';
import { Button } from '../../components/ui/Button';
import { Dialog } from '../../components/ui/Dialog';
import '../../styles/primitives.css';
import { useRouteTransitionCoordinator } from '../../routing/RouteTransitionCoordinator';
import { useDeepLinkedHistory } from '../../routing/useDeepLinkedHistory';
import { ROUTES, deepSearchPath } from '../../routing/paths';
type CrossMarketplacePageProps = { jobId?: string, onReopen?: (jobId: string) => void };
export const CrossMarketplacePage = ({ jobId, onReopen }: CrossMarketplacePageProps) => {
  // Store selectors â€” seletores atÃ´micos para campos que disparam renders pesados (Armadilha 1)
  const loading = useSearchStore((s) => s.cross.loading);
  const results = useSearchStore((s) => s.cross.results);
  const selectedItems = useSearchStore((s) => s.cross.selectedItems);
  const selectionMode = useSearchStore((s) => s.cross.selectionMode);
  // useShallow para mÃºltiplos campos do mesmo slice
  const { targetSku, zipcode } = useSearchStore(
    useShallow((s) => ({
      targetSku: s.cross.targetSku,
      zipcode: s.cross.zipcode,
    }))
  );
  // Actions
  const setCross = useSearchStore((s) => s.setCross);
  const startCrossSearch = useSearchStore((s) => s.startCrossSearch);
  const transition = useRouteTransitionCoordinator();
  const deepHistory = useDeepLinkedHistory('cross', jobId);
  // UI transiente â€” permanecem como useState local (D-03)
  const [loadingShipping, setLoadingShipping] = useState<Record<string, boolean>>({});
  const [exporting, setExporting] = useState(false);
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);

  useEffect(() => {
    if (['ready', 'failed', 'not-found', 'temporary-error', 'wrong-type'].includes(deepHistory.status)) {
      const timer = setTimeout(() => setHistoryRefreshKey(k => k + 1), 0);
      return () => clearTimeout(timer);
    }
  }, [deepHistory.status]);

  // --- HistÃ³rico de buscas: Ã­cone no topo controla o painel (UX-06) ---
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyCount, setHistoryCount] = useState(0);
  // Modal de CEP â€” mesma lÃ³gica da busca comparativa: ao clicar "Calcular Frete" sem CEP,
  // abre o modal em vez de bloquear com alert.
  const [cepModalOpen, setCepModalOpen] = useState(false);
  const [cepDraft, setCepDraft] = useState('');
  const [cepError, setCepError] = useState<string | null>(null);
  const [pendingShipItem, setPendingShipItem] = useState<{ item: any; marketplace: string } | null>(null);
  const cepDraftRef = useRef<HTMLInputElement>(null);
  // --- Modal de detalhes de produto (Quick task 260712-iiz) ---
  const [detailModal, setDetailModal] = useState<{ brand: string; url: string; seed?: any } | null>(null);
  // --- Matriz Regional (FRET-09): modal "Frete por regiÃ£o", 5 CEPs curados, sem input do usuÃ¡rio ---
  // --- ValidaÃ§Ã£o do SKU alvo (UX-07): UX gate only, backend valida independentemente (T-38-04) ---
  const [skuFieldError, setSkuFieldError] = useState<string | null>(null);
  const validateSku = (value: string): boolean => SKU_PATTERN.test(value.trim());
  const isTargetSkuValid = validateSku(targetSku);

  // withDisplayOrder importado do store (fonte Ãºnica â€” CR-01/IN-02): usado pela
  // prÃ©-carga de histÃ³rico (useDeepLinkedHistory) quando o job aceito chega ao
  // terminal â€” startCrossSearch nÃ£o preenche mais results (D-08, job sÃ³ aceito).
  const allItems: any[] = results?.results ?? [];

  const toggleItem = (url: string) => {
    const next = new Set(selectedItems);
    if (next.has(url)) {
      next.delete(url);
    } else {
      next.add(url);
    }
    setCross({ selectedItems: next });
  };

  const isAllSelected = allItems.length > 0 && allItems.every((i: any) => selectedItems.has(i.url));

  const toggleSelectAll = () => {
    if (isAllSelected) {
      setCross({ selectedItems: new Set() });
    } else {
      setCross({ selectedItems: new Set(allItems.map((i: any) => i.url)) });
    }
  };

  // Marketplace brand_key lookup map (D-07 / Pitfall 6)

  const handleAddToMonitor = async (url: string, brand: string) => {
    try {
      const result = await ApiClient.addToMonitor(url, brand);
      if (result.status === 'already_active') {
        toast.info('Produto já está em monitoramento');
      } else if (result.status === 'reactivated') {
        toast.success('Monitor reativado');
      } else {
        toast.success('Adicionado ao monitoramento');
      }
    } catch (err: any) {
      toast.error(err.message || 'Erro ao adicionar ao monitoramento');
    }
  };

  const handleExport = async (mode: 'all' | 'selected') => {
    const itemsToExport = mode === 'all'
      ? allItems
      : allItems.filter((i: any) => selectedItems.has(i.url));
    setExporting(true);
    try {
      await ApiClient.exportCrossMarketplace({
        items: itemsToExport,
        search_query: results?.search_query,
        target_sku: targetSku,
      });
    } catch (err: any) {
      toast.error('Erro ao exportar: ' + err.message);
    } finally {
      setExporting(false);
      setCross({ selectionMode: false, selectedItems: new Set() });
    }
  };


  // NÃºcleo do cÃ¡lculo de frete de UM item (jÃ¡ com CEP validado).
  const runShipForItem = async (item: any, marketplace: string, currentZip: string) => {
    const key = `${marketplace}-${item.url}`;
    setLoadingShipping(prev => ({ ...prev, [key]: true }));
    // Snapshot do result set ANTES do await: se uma nova busca por SKU ou reabertura de
    // histÃ³rico substituir os resultados durante o cÃ¡lculo, nÃ£o splicear o frete no
    // conjunto de OUTRA busca (mesma classe de race do CR-01 â€” WR-01).
    const baseResults = useSearchStore.getState().cross.results;

    try {
      const data = await ApiClient.calculateSingleShipping({
        marketplace,
        url: item.url,
        zipcode: currentZip
      });

      if (data.status === 'success' && data.shipping_info) {
        // LÃª o estado atual do store (equivalente ao prev do useState funcional)
        const prev = useSearchStore.getState().cross.results;
        // Staleness guard (WR-01): result set trocado durante o await â†’ descarta a escrita.
        if (!prev || prev !== baseResults) return;
        const newResults = prev.results.map((r: any) => {
          if (r.url === item.url && r.marketplace === marketplace) {
            const newShippingPrice = data.shipping_info.shipping_price;
            const isFree = data.shipping_info.is_free_shipping;
            return {
              ...r,
              is_free_shipping: isFree,
              shipping_price: newShippingPrice,
              landed_price: r.price + (newShippingPrice || 0),
              // Surface delivery-time (prazo, D-05) quando o engine retorna essas chaves;
              // .get()-equivalente via acesso opcional â€” nunca inventa um fallback novo.
              ...(data.shipping_info.estimated_delivery_days !== undefined ? { estimated_delivery_days: data.shipping_info.estimated_delivery_days } : {}),
              ...(data.shipping_info.delivery_raw_text !== undefined ? { shipping_raw_text: data.shipping_info.delivery_raw_text } : {}),
              _shipping_state: undefined,
            };
          }
          return r;
        });

        // Reavalia o is_buybox_winner com os novos preÃ§os landed
        const minPrice = Math.min(...newResults.map((r: any) => r.landed_price ?? r.price));
        newResults.forEach((r: any) => {
          r.is_buybox_winner = (r.landed_price ?? r.price) === minPrice;
        });

        setCross({ results: { ...prev, results: newResults } });
      } else {
        // Uma cotação sem valor não prova bloqueio. Em particular, o backend
        // classifica CAPTCHA e falhas de extração da sessão de frete como
        // transitórios para não fechar o marketplace inteiro. Preserve esse
        // estado também nesta tela legada de busca comparativa, em vez de
        // convertê-lo em "Bloqueado (anti-bot)" apenas para a Netshoes.
        const prev = useSearchStore.getState().cross.results;
        if (!prev || prev !== baseResults) return;
        const newResults = prev.results.map((r: any) =>
          (r.url === item.url && r.marketplace === marketplace)
            ? { ...r, _shipping_state: 'temporary_failure' }
            : r
        );
        setCross({ results: { ...prev, results: newResults } });
        toast.error(data.message || "Frete temporariamente indisponível");
      }
    } catch (err: any) {
      toast.error("Erro ao calcular frete: " + err.message);
    } finally {
      setLoadingShipping(prev => ({ ...prev, [key]: false }));
    }
  };

  // Entry handler do botÃ£o "Calcular Frete": usa o CEP da sessÃ£o se vÃ¡lido, senÃ£o abre o modal.
  const handleCalculateShipping = (e: React.MouseEvent, item: any, marketplace: string) => {
    e.preventDefault();
    e.stopPropagation();
    const currentZip = zipcode.replace(/\D/g, '');
    if (currentZip.length === 8) {
      runShipForItem(item, marketplace, currentZip);
      return;
    }
    setPendingShipItem({ item, marketplace });
    setCepDraft(zipcode);
    setCepError(null);
    setCepModalOpen(true);
  };

  const confirmCepCross = () => {
    const zip = cepDraft.replace(/\D/g, '');
    if (zip.length !== 8) {
      setCepError('Informe um CEP válido com 8 dígitos.');
      cepDraftRef.current?.focus();
      return;
    }
    const masked = zip.slice(0, 5) + '-' + zip.slice(5);
    setCross({ zipcode: masked });
    setCepModalOpen(false);
    setCepError(null);
    const pending = pendingShipItem;
    setPendingShipItem(null);
    if (pending) runShipForItem(pending.item, pending.marketplace, zip);
  };

  // Matriz Regional (FRET-09): abre o modal jÃ¡ em loading (todas as 5 regiÃµes
  // simultÃ¢neas) e dispara a chamada sem CEP â€” os 5 CEPs curados sÃ£o resolvidos
  // no backend (D-06/D-07, nunca inline durante busca).
  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!targetSku) return;
    if (!validateSku(targetSku)) {
      setSkuFieldError(SKU_ERROR_MSG);
      return;
    }
    if (jobId) transition.navigate(ROUTES.cross, { targetKind: 'cross' });
    // startCrossSearch faz: loading=true, results=null, reset seleÃ§Ã£o, AbortController â€”
    // tudo dentro da action (CR-01). D-08: retorna a aceitaÃ§Ã£o (202), nÃ£o a conclusÃ£o.
    const outcome = await startCrossSearch({
      target_sku: targetSku,
      zipcode: zipcode.replace(/\D/g, '').length === 8 ? zipcode.replace(/\D/g, '') : undefined,
    });
    // D-08: 'success' aqui é o job ACEITO (202) — navega para a identidade durável
    // imediatamente; o worker ainda pode estar rodando (não trata aceitação como conclusão).
    // historyRefreshKey permanece local (D-03) — o job já foi persistido no histórico
    // no momento do 202 (não em cancelamento/erro — WR-06).
    if (outcome.status === 'success') {
      transition.navigate(deepSearchPath('cross', outcome.jobId), { targetKind: 'cross' });
      setHistoryRefreshKey(k => k + 1);
    }
  };

  return (
    <div className="page-content priority-page sku-search-page">
      <HistoryToggleButton
        title="Ver histórico de buscas"
        count={historyCount}
        open={historyOpen}
        onClick={() => setHistoryOpen(o => !o)}
      />
      {onReopen && (
        <HistoryList
          type="cross"
          onReopen={onReopen}
          refreshKey={historyRefreshKey}
          collapsed={!historyOpen}
          onToggleCollapsed={() => setHistoryOpen(o => !o)}
          onCountChange={setHistoryCount}
          hideHeader
        />
      )}

      <GlassCard className="search-bar-container" title="Localizar ofertas" subtitle="Informe o código ou SKU do produto e, se desejar, o CEP para o frete.">
        <form onSubmit={handleSearch} className="search-form">
          <div className="search-main-row">
            <div className="search-field">
              <label className="search-field-label" htmlFor="sku-input">SKU Alvo / Referência</label>
              <div className={`search-input-wrapper${skuFieldError ? ' cep-input-error' : ''}`}>
                <Search className="search-icon" size={20} aria-hidden="true" />
                <input
                  id="sku-input"
                  type="text"
                  className="search-input"
                  placeholder="Ex: ML.05.0326046"
                  value={targetSku}
                  aria-invalid={skuFieldError ? 'true' : 'false'}
                  aria-describedby={skuFieldError ? 'sku-error-msg' : undefined}
                  onChange={e => {
                    const val = e.target.value;
                    setCross({ targetSku: val });
                    if (skuFieldError) setSkuFieldError(null);
                  }}
                  onBlur={e => {
                    const val = e.target.value.trim();
                    if (val && !validateSku(val)) {
                      setSkuFieldError(SKU_ERROR_MSG);
                    }
                  }}
                />
              </div>
              {skuFieldError && (
                <p id="sku-error-msg" className="cep-helper cep-helper-error" role="alert">
                  <AlertTriangle size={12} aria-hidden="true" />
                  {skuFieldError}
                </p>
              )}
            </div>

            <div className="search-field">
              <label className="search-field-label" htmlFor="sku-cep-input">CEP (opcional)</label>
              <div className="search-input-wrapper">
                <MapPin className="search-icon" size={20} aria-hidden="true" />
                <input
                  id="sku-cep-input"
                  type="text"
                  inputMode="numeric"
                  autoComplete="postal-code"
                  className="search-input"
                  placeholder="Ex: 01001-000"
                  value={zipcode}
                  onChange={e => {
                    let val = e.target.value.replace(/\D/g, '');
                    if (val.length > 8) val = val.slice(0, 8);
                    if (val.length > 5) val = val.slice(0, 5) + '-' + val.slice(5);
                    setCross({ zipcode: val });
                  }}
                />
              </div>
            </div>
          </div>
          <p className="sku-search-helper">
            Ao informar o SKU, o sistema identifica automaticamente as especificações do produto na loja base para varrer os demais e-commerces concorrentes.
          </p>
          <Button
            type="submit"
            fullWidth
            className="sku-search-submit mt-4"
            disabled={loading || ['loading', 'pending', 'running', 'retrying', 'interrupted'].includes(deepHistory.status) || !isTargetSkuValid}
            loading={loading || ['loading', 'pending', 'running', 'retrying', 'interrupted'].includes(deepHistory.status)}
            icon={<Radar size={18} />}
          >
            Buscar em Marketplaces
          </Button>
        </form>
      </GlassCard>



      { (loading || ['loading', 'pending', 'running', 'retrying', 'interrupted'].includes(deepHistory.status)) ? (
        <>
          <JobProgressTerminal status={deepHistory.status} progress={deepHistory.progress} error={deepHistory.error} retry={deepHistory.retry} />
          <CrossResultsSkeleton />
        </>
      ) : deepHistory.status === 'failed' ? (
        <JobProgressTerminal status={deepHistory.status} progress={deepHistory.progress} error={deepHistory.error} retry={deepHistory.retry} />
      ) : results && (
        <div className="sku-results-stack">


          {results.reference_product && (
            <GlassCard title="Produto Referência (Loja Base)">
              <div className="sku-reference">
                {results.reference_product.image_url ? (
                  <img
                    src={results.reference_product.image_url}
                    alt={results.reference_product.name}
                    className="sku-reference-image"
                  />
                ) : (
                  <div className="sku-reference-image sku-reference-placeholder">
                    <Package size={32} className="text-muted" />
                  </div>
                )}
                <div>
                  <h3 className="sku-reference-title">{results.reference_product.name}</h3>
                  <p className="sku-reference-price">R$ {results.reference_product.price.toFixed(2)}</p>
                  <a href={results.reference_product.url} target="_blank" rel="noopener noreferrer" className="sku-reference-link">
                    Ver no site <ExternalLink size={12} />
                  </a>
                </div>
              </div>
            </GlassCard>
          )}

          {/* Export toolbar */}
          <div className="sku-export-toolbar" style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '16px' }}>
            {selectionMode ? (
              <>
                <label className={`stock-toggle stock-toggle--clickable${isAllSelected ? ' active' : ''}`}>
                  <input type="checkbox" checked={isAllSelected} onChange={toggleSelectAll} />
                  <span className="stock-toggle-box">
                    {isAllSelected && <Check size={12} />}
                  </span>
                  Selecionar todos
                </label>
                <span className={`sku-export-counter${selectedItems.size > 0 ? ' has-selection' : ''}`}>
                  {selectedItems.size} selecionado(s)
                </span>
                <div className="toolbar-spacer" />
                <Button
                  onClick={() => handleExport('all')}
                  disabled={exporting}
                  icon={<FileSpreadsheet size={16} />}
                >
                  Exportar todos
                </Button>
                <Button
                  className="btn-excel"
                  onClick={() => handleExport('selected')}
                  disabled={exporting || selectedItems.size === 0}
                  icon={<FileSpreadsheet size={16} />}
                >
                  Exportar selecionados ({selectedItems.size})
                </Button>
                <Button
                  variant="outline"
                  onClick={() => { setCross({ selectionMode: false, selectedItems: new Set() }); }}
                  disabled={exporting}
                >
                  Cancelar
                </Button>
              </>
            ) : (
              <>
                <div className="toolbar-spacer" />
                <Button
                  className="btn-excel"
                  onClick={() => setCross({ selectionMode: true })}
                  disabled={exporting}
                  icon={<FileSpreadsheet size={16} />}
                >
                  Exportar Excel
                </Button>
              </>
            )}
          </div>

          <div className="sku-market-grid">
            {['Mercado Livre', 'Netshoes', 'Amazon'].map(marketplace => {
              const marketResults = (results.results || [])
                .map((r: any, index: number) => ({ ...r, _render_order: r._display_order ?? index }))
                .filter((r: any) => r.marketplace === marketplace);

              marketResults.sort((a: any, b: any) => {
                return a._render_order - b._render_order;
              });

              return (
                <GlassCard key={marketplace} title={marketplace}>
                  {marketResults.length > 0 ? (
                    <div className="sku-offer-list">
                      {marketResults.map((item: any, i: number) => (
                        <div
                          key={`${marketplace}-${item.url || i}`}
                          className={`sku-offer-card${item.is_buybox_winner ? ' is-buybox' : ''}${selectionMode && selectedItems.has(item.url) ? ' is-selected' : ''}`}
                          role="button"
                          tabIndex={0}
                          aria-label={`${item.title}. ${item.is_buybox_winner ? 'Menor preço. ' : ''}${selectionMode && selectedItems.has(item.url) ? 'Selecionado' : 'Abrir detalhes'}`}
                          onClick={() => {
                            if (selectionMode) {
                              toggleItem(item.url);
                              return;
                            }
                            setDetailModal({ brand: marketplace, url: item.url, seed: item });
                          }}
                          onKeyDown={(event) => {
                            if (event.target !== event.currentTarget) return;
                            if (event.key !== 'Enter' && event.key !== ' ') return;
                            event.preventDefault();
                            if (selectionMode) toggleItem(item.url);
                            else setDetailModal({ brand: marketplace, url: item.url, seed: item });
                          }}
                        >
                          {selectionMode && (
                            <label
                              className="card-select-checkbox"
                              onClick={e => { e.preventDefault(); e.stopPropagation(); toggleItem(item.url); }}
                            >
                              <input
                                type="checkbox"
                                checked={selectedItems.has(item.url)}
                                readOnly
                                tabIndex={-1}
                              />
                              <span className="stock-toggle-box">
                                {selectedItems.has(item.url) && <Check size={12} />}
                              </span>
                            </label>
                          )}
                          <div>
                            <p className="sku-offer-title">
                              {item.is_similar && (
                                <span
                                  title="Produto similar (não é o match exato da marca buscada)"
                                  className="sku-offer-similar"
                                >
                                  Similar
                                </span>
                              )}
                              {item.title}
                            </p>
                            <p className="sku-offer-seller">Vendedor: {item.seller}</p>

                          </div>
                          {item.image_url && (
                            <div className="sku-offer-image">
                              <img src={item.image_url} alt={item.title} />
                            </div>
                          )}
                          <div className="sku-offer-price-row">
                            <span className={`sku-offer-price${item.is_buybox_winner ? ' is-buybox' : ''}`}>
                              R$ {(item.landed_price ?? item.price).toFixed(2)}
                              {item.shipping_price === null && !item.is_free_shipping && (
                                <span className="sku-shipping-pending-icon" title="Frete a calcular">⚠️</span>
                              )}
                            </span>
                            {item.is_free_shipping ? (
                              <span className="sku-offer-badge sku-offer-badge--free">
                                Frete Grátis
                              </span>
                            ) : item.is_buybox_winner ? (
                              <span className="sku-offer-badge sku-offer-badge--buybox">
                                <CheckCircle2 size={10} /> Menor Preço
                              </span>
                            ) : null}
                          </div>
                          <div className="sku-offer-shipping">
                            {item._shipping_state === 'blocked' ? (
                              <span className="monitor-price-blocked" title="Não foi possível calcular o frete.">
                                Bloqueado (anti-bot)
                              </span>
                            ) : item._shipping_state === 'temporary_failure' ? (
                              <>
                                <span className="sku-shipping-pending">Frete temporariamente indisponível</span>
                                <button
                                  type="button"
                                  className="sku-shipping-button"
                                  onClick={(e) => handleCalculateShipping(e, item, marketplace)}
                                  disabled={loadingShipping[`${marketplace}-${item.url}`]}
                                >
                                  <RefreshCw size={10} /> Tentar novamente
                                </button>
                              </>
                            ) : item.shipping_price === null && !item.is_free_shipping ? (
                              <>
                                <span className="sku-shipping-pending">Frete a calcular</span>
                                <button
                                  type="button"
                                  className="sku-shipping-button"
                                  onClick={(e) => handleCalculateShipping(e, item, marketplace)}
                                  disabled={loadingShipping[`${marketplace}-${item.url}`]}
                                >
                                  {loadingShipping[`${marketplace}-${item.url}`] ? <RefreshCw size={10} className="animate-spin" /> : <Package size={10} />}
                                  {loadingShipping[`${marketplace}-${item.url}`] ? "Calculando..." : "Calcular Frete"}
                                </button>
                              </>
                            ) : (
                              <span>
                                R$ {item.price.toFixed(2)} (Produto) + R$ {(item.shipping_price || 0).toFixed(2)} (Frete)
                                {(() => {
                                  const estimateText = item.shipping_raw_text || (item.estimated_delivery_days ? `Até ${item.estimated_delivery_days} dias úteis` : '');
                                  return estimateText ? <span className="shipping-estimate shipping-estimate--block">{estimateText}</span> : null;
                                })()}
                              </span>
                            )}
                          </div>
                          <div className="sku-offer-actions">
                            <button
                              type="button"
                              className="btn-icon btn-sm"
                              title="Adicionar ao monitoramento"
                              onClick={async (e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                await handleAddToMonitor(item.url, "unknown");
                              }}
                            >
                              <Plus size={14} />
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (() => {
                    const marketError = (results.errors || []).find((e: any) => e.marketplace === marketplace);
                    if (marketError) {
                      const reason = marketError.reason || '';
                      const isBlocked = reason.toLowerCase().includes('blocked') || reason.includes('429') || reason.includes('403');
                      const isNotFound = reason.toLowerCase().includes('not found') || reason.includes('INITIAL_STATE');
                      return (
                        <div className="empty-state sku-empty-state" style={{ color: 'var(--danger, #dc2626)' }}>
                          <AlertTriangle size={24} style={{ marginBottom: '8px', color: 'var(--danger, #dc2626)' }} />
                          <p><strong>Falha na busca</strong></p>
                          <p style={{ fontSize: '0.9rem', marginTop: '4px', opacity: 0.9 }}>
                            {isBlocked
                              ? "O acesso foi bloqueado pelo marketplace (anti-bot). Tente novamente mais tarde."
                              : isNotFound
                                ? "Produto não localizado na base de dados deste marketplace."
                                : "Não foi possível conectar com este marketplace no momento."}
                          </p>
                        </div>
                      );
                    }
                    return (
                      <div className="empty-state sku-empty-state">
                        <p>Nenhum produto atendeu ao critério estrito de busca.</p>
                      </div>
                    );
                  })()}
                </GlassCard>
              )
            })}
          </div>

        </div>
      )}

      {/* Modal de CEP â€” pede o CEP ao calcular frete sem ter informado um (mesma lÃ³gica da comparativa) */}
      {cepModalOpen && (
        <Dialog
          title={<><MapPin size={20} aria-hidden="true" /> Calcular frete</>}
          description="Informe o CEP de entrega para calcular o frete."
          onClose={() => setCepModalOpen(false)}
          initialFocusRef={cepDraftRef}
          size="sm"
        >
          <div className={`search-input-wrapper${cepError ? ' cep-input-error' : ''}`}>
            <MapPin className="search-icon" size={20} aria-hidden="true" />
            <input
              ref={cepDraftRef}
              type="text"
              inputMode="numeric"
              autoComplete="postal-code"
              className="search-input"
              placeholder="00000-000"
              value={cepDraft}
              autoFocus
              aria-invalid={cepError ? 'true' : 'false'}
              onChange={(e) => {
                let val = e.target.value.replace(/\D/g, '');
                if (val.length > 8) val = val.slice(0, 8);
                if (val.length > 5) val = val.slice(0, 5) + '-' + val.slice(5);
                setCepDraft(val);
                if (cepError) setCepError(null);
              }}
              onKeyDown={(e) => { if (e.key === 'Enter') confirmCepCross(); }}
            />
          </div>
          {cepError && (
            <p className="cep-helper cep-helper-error cep-modal-error" role="alert" aria-live="polite">
              <AlertTriangle size={12} aria-hidden="true" /> {cepError}
            </p>
          )}
          <div className="dialog-actions dialog-actions--equal">
            <Button variant="outline" onClick={() => setCepModalOpen(false)}>Cancelar</Button>
            <Button onClick={confirmCepCross}>Calcular</Button>
          </div>
        </Dialog>
      )}

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
