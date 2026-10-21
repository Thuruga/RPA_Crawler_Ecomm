/* eslint-disable @typescript-eslint/no-explicit-any */
import React, { useState, useEffect, useRef } from 'react';
import {
  Search,
  Package,
  CheckCircle2,
  Plus,
  RefreshCw,
  XCircle,
  ExternalLink,
  FileSpreadsheet,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  MapPin,
  Truck,
} from 'lucide-react';
import { toast } from 'sonner';
import { getProductPriceView, type BrandItem } from '../../shared/lib';
import { JobProgressTerminal } from '../../shared/ui/JobProgressTerminal';
import { ResultsSkeleton } from './Skeletons';
import { HistoryList, HistoryToggleButton } from '../history/HistoryList';
import { GlassCard } from '../../shared/ui/GlassCard';
import { ProductDetailModal } from '../product/ProductDetailModal';
import { ApiClient } from '../../api/client';
import { useSearchStore } from '../../stores/searchStore';
import { useShallow } from 'zustand/react/shallow';
import { EmptyState, Status } from '../../components/ui/Feedback';
import { TableRegion } from '../../components/ui/TableRegion';
import { Button } from '../../components/ui/Button';
import { Dialog } from '../../components/ui/Dialog';
import '../../styles/primitives.css';
import { useRouteTransitionCoordinator } from '../../routing/RouteTransitionCoordinator';
import { useDeepLinkedHistory } from '../../routing/useDeepLinkedHistory';
import { ROUTES, deepSearchPath } from '../../routing/paths';

type SearchPageProps = {
  brands: BrandItem[];
  jobId?: string;
  onReopen?: (jobId: string) => void;
};

export const SearchPage = ({ brands, jobId, onReopen }: SearchPageProps) => {
  // Store selectors   seletores atômicos para campos que disparam renders pesados (Armadilha 1)
  const loading = useSearchStore((s) => s.search.loading);
  const results = useSearchStore((s) => s.search.results);
  // useShallow para múltiplos campos do mesmo slice (evita re-render por referência nova do objeto)
  const { query, sort, zipcode, selectedBrands } = useSearchStore(
    useShallow((s) => ({
      query: s.search.query,
      sort: s.search.sort,
      zipcode: s.search.zipcode,
      selectedBrands: s.search.selectedBrands,
    }))
  );
  // Actions
  const setSearch = useSearchStore((s) => s.setSearch);
  const startSearch = useSearchStore((s) => s.startSearch);
  const transition = useRouteTransitionCoordinator();
  const deepHistory = useDeepLinkedHistory('search', jobId);
  // UI transiente   permanecem como useState local (D-03)
  const [exporting, setExporting] = useState(false);
  // A escolha da quantidade pertence somente a esta tela: 10 mantém o contrato
  // histórico, "Tudo" vira null apenas no payload.
  const [quantityValue, setQuantityValue] = useState('10');
  const [quantityError, setQuantityError] = useState<string | null>(null);
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);

  useEffect(() => {
    if (['ready', 'failed', 'not-found', 'temporary-error', 'wrong-type'].includes(deepHistory.status)) {
      const timer = setTimeout(() => setHistoryRefreshKey(k => k + 1), 0);
      return () => clearTimeout(timer);
    }
  }, [deepHistory.status]);

  // --- Histórico de buscas: ícone no topo controla o painel (UX-06) ---
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyCount, setHistoryCount] = useState(0);
  // --- Campo de CEP da barra de busca (opcional, sem valor padrão) ---
  // Se preenchido antes da busca, o frete de TODOS os produtos vem junto (include_shipping).
  const [cepFieldError, setCepFieldError] = useState<string | null>(null);
  const cepFieldRef = useRef<HTMLInputElement>(null);
  // --- Frete sob demanda (modal de CEP + cálculo de um único produto) ---
  const [cepModalOpen, setCepModalOpen] = useState(false);
  const [cepDraft, setCepDraft] = useState('');
  const [cepError, setCepError] = useState<string | null>(null);
  // pendingCalc: o que calcular assim que o usuário confirmar o CEP no modal.
  const [pendingCalc, setPendingCalc] = useState<
    | { type: 'all' }
    | { type: 'one'; brandKey: string; product: any; key: string }
    | null
  >(null);
  const [loadingShipping, setLoadingShipping] = useState<Record<string, boolean>>({});
  const [expandedShipping, setExpandedShipping] = useState<Record<string, boolean>>({});
  const cepDraftRef = useRef<HTMLInputElement>(null);
  // --- Modal de detalhes de produto (Quick task 260712-iiz) ---
  const [detailModal, setDetailModal] = useState<{ brand: string; url: string; seed?: any } | null>(null);
  // --- Matriz Regional (FRET-09): modal "Frete por região", 5 CEPs curados, sem input do usuário ---

  const toggleBrand = (key: string) => {
    const next = selectedBrands.includes(key)
      ? selectedBrands.filter(k => k !== key)
      : [...selectedBrands, key];
    setSearch({ selectedBrands: next });
  };

  const selectAllBrands = () => {
    // Só marcas ativas são alvos válidos de /search (inativas �  400). Espelha BannersPage.
    setSearch({ selectedBrands: brands.filter(b => b.is_active !== false).map(b => b.brand_key) });
  };

  const clearBrands = () => {
    setSearch({ selectedBrands: [] });
  };

  const resolveMaxPerBrand = (): number | null | undefined => {
    const value = quantityValue.trim();
    if (value === 'all') return null;
    if (!/^\d+$/.test(value) || Number(value) < 1 || !Number.isSafeInteger(Number(value))) {
      setQuantityError('Escolha uma opção ou informe uma quantidade inteira positiva.');
      return undefined;
    }
    setQuantityError(null);
    return Number(value);
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    const maxPerBrand = resolveMaxPerBrand();
    if (maxPerBrand === undefined) return;
    // CEP é OPCIONAL. Se o usuário preencher um CEP válido ANTES da busca, o frete de
    // todos os produtos vem junto (include_shipping). Sem CEP �  busca só preços, e o
    // frete fica sob demanda via "Calcular Frete" por produto.
    const cepDigits = zipcode.replace(/\D/g, '');
    if (cepDigits.length > 0 && cepDigits.length < 8) {
      setCepFieldError('Informe um CEP válido com 8 dígitos.');
      cepFieldRef.current?.focus();
      return;
    }
    setCepFieldError(null);
    if (jobId) transition.navigate(ROUTES.search, { targetKind: 'search' });
    const outcome = await startSearch({
      query,
      sort,
      brands: selectedBrands.length > 0 ? selectedBrands : undefined,
      zipcode: cepDigits.length === 8 ? cepDigits : undefined,
      include_shipping: cepDigits.length === 8 ? true : undefined,
      max_per_brand: maxPerBrand,
    });
    // D-08: 'success' aqui é o job ACEITO (202), não concluído � navega para a
    // identidade durável imediatamente; o worker ainda pode estar rodando.
    // historyRefreshKey permanece local (D-03) � o job já foi persistido no
    // histórico no momento do 202 (não em cancelamento/erro � WR-06).
    if (outcome.status === 'success') {
      transition.navigate(deepSearchPath('search', outcome.jobId), { targetKind: 'search' });
      setHistoryRefreshKey(k => k + 1);
    }
  };

  const handleExport = async () => {
    if (!query) return;
    const maxPerBrand = resolveMaxPerBrand();
    if (maxPerBrand === undefined) return;
    const cepDigits = zipcode.replace(/\D/g, '');
    if (cepDigits.length > 0 && cepDigits.length < 8) {
      setCepFieldError('Informe um CEP válido com 8 dígitos.');
      cepFieldRef.current?.focus();
      return;
    }
    setCepFieldError(null);
    setExporting(true);
    try {
      await ApiClient.exportSearch({
        query,
        sort,
        brands: selectedBrands.length > 0 ? selectedBrands : undefined,
        zipcode: cepDigits.length === 8 ? cepDigits : undefined,
        include_shipping: cepDigits.length === 8 ? true : undefined,
        max_per_brand: maxPerBrand,
      });
    } catch (err: any) {
      console.error(err);
      toast.error('Erro ao exportar: ' + err.message);
    } finally {
      setExporting(false);
    }
  };

  // -------------------------------------------------------------------------
  // Adicionar ao monitoramento
  // -------------------------------------------------------------------------
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

  // -------------------------------------------------------------------------
  // Frete sob demanda (modal de CEP + cálculo por produto VTEX)
  // -------------------------------------------------------------------------

  // Achata todos os produtos com frete sob demanda suportado nos resultados atuais.
  const shippingProductsInResults = (): Array<{ brandKey: string; p: any }> => {
    const out: Array<{ brandKey: string; p: any }> = [];
    const rows = results && Array.isArray(results.results) ? results.results : [];
    for (const row of rows) {
      const meta = brands.find(b => b.brand_key === row.brand_key);
      const engine = meta?.engine?.toLowerCase();
      for (const p of (row.products || [])) {
        if (engine === 'vtex' && p.sku_id) out.push({ brandKey: row.brand_key, p });
        if (engine && ['shopify', 'wake', 'mercadolivre', 'amazon', 'netshoes', 'linx', 'zara', 'dafiti', 'algolia', 'sfcc', 'lacoste', 'privalia'].includes(engine)) {
          out.push({ brandKey: row.brand_key, p });
        }
      }
    }
    return out;
  };

  // Aplica o resultado da simulação de frete a um produto (por url) dentro de search.results.
  const applyShippingToProduct = (productUrl: string, data: { state: string; shipping_options: any[]; shipping?: any; shipping_price?: number | null; is_free_shipping?: boolean }) => {
    const cur = useSearchStore.getState().search.results;
    if (!cur || !Array.isArray(cur.results)) return;
    const newResults = cur.results.map((row: any) => ({
      ...row,
      products: (row.products || []).map((p: any) => {
        if (p.url !== productUrl) return p;
        const opts = data.shipping_options || [];
        const primary = opts.length > 0 ? opts[0] : null;
        return {
          ...p,
          _shipping_state: data.state,
          shipping_options: opts,
          shipping: primary || data.shipping || null,
          shipping_price: primary ? primary.price : (data.shipping_price ?? null),
          is_free_shipping: primary ? primary.is_free_shipping : (data.is_free_shipping ?? false),
        };
      }),
    }));
    setSearch({ results: { ...cur, results: newResults } });
  };

  const runCalcOne = async (brandKey: string, product: any, key: string, zip: string) => {
    setLoadingShipping(prev => ({ ...prev, [key]: true }));
    try {
      const meta = brands.find(b => b.brand_key === brandKey);
      const data = meta?.engine?.toLowerCase() === 'vtex'
        ? await ApiClient.calculateVtexShipping({
          brand_key: brandKey,
          sku_id: product.sku_id,
          seller_id: product.seller_id || '1',
          zipcode: zip,
        })
        : await ApiClient.calculateShippingBrand({
          brand_key: brandKey,
          product_url: product.url,
          zipcode: zip,
          price: product.price_discount ?? product.price_full ?? product.price ?? null,
        });
      applyShippingToProduct(key, data);
      setExpandedShipping(prev => ({ ...prev, [key]: true }));
    } catch (err: any) {
      toast.error('Erro ao calcular frete: ' + err.message);
    } finally {
      setLoadingShipping(prev => ({ ...prev, [key]: false }));
    }
  };

  const runCalcAll = async (zip: string) => {
    const targets = shippingProductsInResults();
    if (targets.length === 0) {
      toast.info('Nenhum produto com frete suportado para calcular.');
      return;
    }
    await Promise.all(
      targets.map(({ brandKey, p }) =>
        runCalcOne(brandKey, p, p.url, zip)
      )
    );
  };

  // Abre o modal de CEP ou calcula direto se o CEP da sessão já é válido.
  const requestCalc = (calc: { type: 'all' } | { type: 'one'; brandKey: string; product: any; key: string }) => {
    const zip = zipcode.replace(/\D/g, '');
    if (zip.length === 8) {
      if (calc.type === 'one') runCalcOne(calc.brandKey, calc.product, calc.key, zip);
      else runCalcAll(zip);
      return;
    }
    setPendingCalc(calc);
    setCepDraft(zipcode);
    setCepError(null);
    setCepModalOpen(true);
  };

  const confirmCep = () => {
    const zip = cepDraft.replace(/\D/g, '');
    if (zip.length !== 8) {
      setCepError('Informe um CEP válido com 8 dígitos.');
      cepDraftRef.current?.focus();
      return;
    }
    const masked = zip.slice(0, 5) + '-' + zip.slice(5);
    setSearch({ zipcode: masked });
    setCepModalOpen(false);
    setCepError(null);
    const pc = pendingCalc;
    setPendingCalc(null);
    if (pc?.type === 'one') runCalcOne(pc.brandKey, pc.product, pc.key, zip);
    else if (pc?.type === 'all') runCalcAll(zip);
  };

  // Matriz Regional (FRET-09): abre o modal já em loading (todas as 5 regiões
  // simultâneas) e dispara a chamada sem CEP � os 5 CEPs curados são resolvidos
  // no backend (D-06/D-07, nunca inline durante busca).
  // Expandir / recolher todos os fretes já calculados.
  const setAllExpanded = (value: boolean) => {
    const next: Record<string, boolean> = {};
    for (const { p } of shippingProductsInResults()) {
      if (p._shipping_state || (Array.isArray(p.shipping_options) && p.shipping_options.length > 0)) {
        next[p.url] = value;
      }
    }
    setExpandedShipping(next);
  };

  return (
    <div className="page-content priority-page comparative-search-page">
      <HistoryToggleButton
        title="Ver histórico de buscas"
        count={historyCount}
        open={historyOpen}
        onClick={() => setHistoryOpen(o => !o)}
      />
      {onReopen && (
        <HistoryList
          type="search"
          onReopen={onReopen}
          refreshKey={historyRefreshKey}
          collapsed={!historyOpen}
          onToggleCollapsed={() => setHistoryOpen(o => !o)}
          onCountChange={setHistoryCount}
          hideHeader
        />
      )}

      <GlassCard className="search-bar-container" title="Parâmetros da busca" subtitle="Defina o produto, o CEP opcional e as marcas que entram na comparação.">
        <form onSubmit={handleSearch} className="search-form" id="search-compare-form">
          <div className="search-main-row">
            <div className="search-field">
              <label className="search-field-label">O que você procura?</label>
              <div className="search-input-wrapper">
                <Search className="search-icon" size={20} />
                <input
                  id="comparative-query"
                  aria-label="Produto para comparar"
                  type="text"
                  className="search-input"
                  placeholder="Ex: Polo Piquet, Camisa Social..."
                  value={query}
                  onChange={(e) => setSearch({ query: e.target.value })}
                />
              </div>
            </div>

            <div className="search-field">
              <label className="search-field-label" htmlFor="cep-input">CEP de entrega (opcional)</label>
              <div className={`search-input-wrapper${cepFieldError ? ' cep-input-error' : ''}`}>
                <MapPin className="search-icon" size={20} aria-hidden="true" />
                <input
                  id="cep-input"
                  ref={cepFieldRef}
                  type="text"
                  inputMode="numeric"
                  autoComplete="postal-code"
                  className="search-input"
                  placeholder="00000-000"
                  value={zipcode}
                  aria-invalid={cepFieldError ? 'true' : 'false'}
                  aria-describedby={cepFieldError ? 'cep-error-msg' : 'cep-helper-msg'}
                  onChange={(e) => {
                    let val = e.target.value.replace(/\D/g, '');
                    if (val.length > 8) val = val.slice(0, 8);
                    if (val.length > 5) {
                      val = val.slice(0, 5) + '-' + val.slice(5);
                    }
                    setSearch({ zipcode: val });
                    if (cepFieldError) setCepFieldError(null);
                  }}
                />
              </div>
              {cepFieldError ? (
                <p id="cep-error-msg" className="cep-helper cep-helper-error" role="alert" aria-live="polite">
                  <AlertTriangle size={12} aria-hidden="true" />
                  {cepFieldError}
                </p>
              ) : null}
            </div>
          </div>

          <div className="search-control-row">
            <div className="search-field">
              <label className="search-field-label">Ordenação</label>
              <select aria-label="Ordenacao dos resultados" className="input" value={sort} onChange={e => setSearch({ sort: e.target.value })}>
                <option value="relevance">Relevância</option>
                <option value="recent">Mais Recentes</option>
                <option value="price_asc">Menor Preço</option>
                <option value="price_desc">Maior Preço</option>
                <option value="top_selling">Mais Vendidos</option>
              </select>
            </div>
            <div className="search-field">
              <label className="search-field-label" htmlFor="max-per-brand">Quantidade por marca</label>
              <select id="max-per-brand" aria-label="Quantidade por marca" className="input" value={quantityValue} aria-invalid={quantityError ? 'true' : 'false'} aria-describedby={quantityError ? 'quantity-error-msg' : undefined} onChange={e => { setQuantityValue(e.target.value); if (quantityError) setQuantityError(null); }}>
                <option value="10">10</option>
                <option value="20">20</option>
                <option value="30">30</option>
                <option value="40">40</option>
                <option value="50">50</option>
                <option value="all">Tudo</option>
              </select>
              {quantityError ? <p id="quantity-error-msg" role="alert" aria-live="polite" className="cep-helper cep-helper-error">{quantityError}</p> : null}
            </div>
          </div>
        </form>

        <div className="search-divider" />

        <div className="brand-filter-panel">
          <div className="brand-filter-header">
            <div>
              <h3 className="brand-filter-title">Filtro de Marcas</h3>
              <p className="brand-filter-caption">
                {selectedBrands.length === 0 ? 'Buscando em todas as marcas ativas no sistema' : `${selectedBrands.length} marca(s) selecionada(s)`}
              </p>
            </div>
            <div className="brand-filter-actions">
              <Button variant="outline" size="sm" onClick={selectAllBrands}>Selecionar Todas</Button>
              <Button variant="outline" size="sm" onClick={clearBrands}>Limpar Seleção</Button>
            </div>
          </div>

          <div className="search-brand-grid brand-selector-grid">
            {brands.filter(b => b.is_active !== false).length === 0 ? (
              <div style={{ gridColumn: '1 / -1', padding: '16px', textAlign: 'center', color: 'var(--text-muted, #888)', fontSize: '0.9rem' }}>
                Carregando marcas cadastradas...
              </div>
            ) : (
              brands.filter(b => b.is_active !== false).map(b => (
                <button
                  type="button"
                  key={b.brand_key}
                  className={`brand-chip ${selectedBrands.includes(b.brand_key) ? 'active' : ''}`}
                  aria-pressed={selectedBrands.includes(b.brand_key)}
                  onClick={() => toggleBrand(b.brand_key)}
                >
                  <div className="brand-chip-icon">
                    <img
                      src={b.logo_url || `https://www.google.com/s2/favicons?domain=${b.domain}&sz=64`}
                      alt={b.brand_name}
                      onError={(e: any) => { e.target.src = `https://ui-avatars.com/api/?name=${b.brand_name}&background=000000&color=fff`; }}
                    />
                  </div>
                  <span>{b.brand_name}</span>
                </button>
              ))
            )}
          </div>

          <Button
            type="submit"
            form="search-compare-form"
            fullWidth
            className="mt-4"
            disabled={loading || ['loading', 'pending', 'running', 'retrying', 'interrupted'].includes(deepHistory.status) || exporting}
            loading={loading || ['loading', 'pending', 'running', 'retrying', 'interrupted'].includes(deepHistory.status)}
            loadingLabel="Buscando..."
            icon={<Search size={18} aria-hidden="true" />}
          >
            Buscar produtos
          </Button>
        </div>
      </GlassCard>

      {results && !loading && (() => {
        const shippingProds = shippingProductsInResults();
        const anyCalculated = shippingProds.some(({ p }) =>
          p._shipping_state || (Array.isArray(p.shipping_options) && p.shipping_options.length > 0) || p.shipping
        );

        return (
          <div className="results-toolbar">
            {shippingProds.length > 0 && (
              <div className="shipping-controls-bar">
                <button
                  type="button"
                  className="btn btn-sm btn-outline"
                  onClick={() => requestCalc({ type: 'all' })}
                >
                  <Truck size={14} aria-hidden="true" /> Calcular frete de todos
                </button>
                {anyCalculated && (
                  <>
                    <Button variant="outline" size="sm" onClick={() => setAllExpanded(true)}>Expandir todos</Button>
                    <Button variant="outline" size="sm" onClick={() => setAllExpanded(false)}>Recolher todos</Button>
                  </>
                )}
              </div>
            )}
            <button
              type="button"
              className="btn btn-excel"
              onClick={handleExport}
              disabled={exporting || !query}
              title="Exportar busca para Excel"
            >
              {exporting ? <RefreshCw className="animate-spin" size={18} /> : <FileSpreadsheet size={18} />}
              {exporting ? "Exportando..." : "Exportar Excel"}
            </button>
          </div>
        );
      })()}

      <TableRegion label="Resultados comparativos por marca" className="results-region">
        { (loading || ['loading', 'pending', 'running', 'retrying', 'interrupted'].includes(deepHistory.status)) ? (
          <>
            <JobProgressTerminal status={deepHistory.status} progress={deepHistory.progress} error={deepHistory.error} retry={deepHistory.retry} />
            <ResultsSkeleton />
          </>
        ) : deepHistory.status === 'failed' ? (
          <JobProgressTerminal status={deepHistory.status} progress={deepHistory.progress} error={deepHistory.error} retry={deepHistory.retry} />
        ) : (
          <div className="results-container">
            {results && (() => {
            // When no brand filter: use brand keys from actual results (includes Amazon, ML, NS)
            const brandKeysToShow = selectedBrands.length > 0
              ? selectedBrands
              : (Array.isArray(results.results) ? results.results.map((r: any) => r.brand_key) : brands.map(b => b.brand_key));


            return (
              <>
                {brandKeysToShow.map((brandKey: string) => {
                  const brand = brands.find(b => b.brand_key === brandKey);
                  const brandRes = Array.isArray(results.results) ? results.results.find((r: any) => r.brand_key === brandKey) : null;
                  const products = brandRes?.products || [];
                  const isVtex = brand?.engine?.toLowerCase() === 'vtex';
                  const isBrandShippingSupported = Boolean(brand?.engine && [
                    'shopify',
                    'wake',
                    'mercadolivre',
                    'amazon',
                    'netshoes',
                    'linx',
                    'zara',
                    'dafiti',
                    'algolia',
                    'sfcc',
                    'lacoste',
                    'privalia',
                  ].includes(brand.engine.toLowerCase()));

                  return (
                    <div key={brandKey} className="brand-column">
                      <h4 className="brand-header">{brand?.brand_name || brandKey}</h4>
                      {brandRes?.error && (
                        <Status tone="warning" icon={<AlertTriangle size={18} aria-hidden="true" />}>{brandRes.error}</Status>
                      )}
                      <div className="product-grid">
                        {products.map((p: any) => (
                          <div
                            key={p.url}
                            className="product-card"
                            role="button"
                            tabIndex={0}
                            onClick={() => setDetailModal({ brand: brandKey, url: p.url, seed: p })}
                            onKeyDown={(event) => {
                              if (event.target !== event.currentTarget) return;
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                setDetailModal({ brand: brandKey, url: p.url, seed: p });
                              }
                            }}
                          >
                            <div className="product-image">
                              {p.image_url ? <img src={p.image_url} alt={p.product_name} /> : <Package size={40} />}
                              {(() => {
                                const priceView = getProductPriceView(p);
                                return priceView.discountPct != null
                                  ? <span className="badge-discount">{priceView.discountPct}% OFF</span>
                                  : null;
                              })()}
                            </div>
                            <div className="product-details">
                              <div className="product-card-heading">
                                <p className="product-name">{p.product_name}</p>
                                <ExternalLink size={14} className="text-muted product-external-icon" />
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
                              <div className="product-meta">
                                {p.available ? <CheckCircle2 size={14} className="text-success" /> : <XCircle size={14} className="text-error" />}
                                <span>{p.available ? 'Em estoque' : 'Esgotado'}</span>
                              </div>

                              {/* Frete: botão sob demanda (sem CEP/cálculo) OU resumo colapsável (já calculado).
                            Detalhe fica colapsado por padrão para não poluir o card. */}
                              {(() => {
                                const opts = Array.isArray(p.shipping_options) ? p.shipping_options : null;
                                const hasOptions = !!opts && opts.length > 0;
                                const isLoading = !!loadingShipping[p.url];
                                const isExpanded = !!expandedShipping[p.url];
                                const calculated = !!p._shipping_state || hasOptions || !!p.shipping;
                                const isBlocked = p._shipping_state === 'blocked';



                                // Estado de loading
                                if (isLoading) {
                                  return (
                                    <div className="shipping-section">
                                      <div className="shipping-loading">
                                        <RefreshCw size={13} className="animate-spin" aria-hidden="true" /> Calculando frete⬦
                                      </div>
                                    </div>
                                  );
                                }

                                // Bloqueado (anti-bot, ex. Netshoes) �  estado explícito, nunca spinner
                                // eterno; o botão Matriz Regional permanece visível (D-07).
                                if (isBlocked) {
                                  return (
                                    <div className="shipping-section">
                                      <div className="shipping-state-row shipping-state-blocked">
                                        <AlertTriangle size={13} aria-hidden="true" />
                                        <span>Bloqueado (anti-bot)</span>
                                      </div>
                                    </div>
                                  );
                                }

                                // Stale (leitura cacheada válida, não uma falha) �  sem botão de
                                // nova tentativa, diferente do estado de falha temporária abaixo.
                                if (p._shipping_state === 'stale') {
                                  return (
                                    <div className="shipping-section">
                                      <div className="shipping-state-row shipping-state-stale">
                                        <AlertTriangle size={13} aria-hidden="true" />
                                        <span>Frete pode estar desatualizado � última cotação mantida</span>
                                      </div>
                                    </div>
                                  );
                                }

                                // Ainda não calculado �  botão sob demanda (VTEX com sku apenas)
                                if (!calculated) {
                                  if (!(isBrandShippingSupported || (isVtex && p.sku_id))) {
                                    return <div className="shipping-section"></div>;
                                  }
                                  return (
                                    <div className="shipping-section">
                                      <div className="shipping-actions-row">
                                        <button
                                          type="button"
                                          className="shipping-calc-btn"
                                          onClick={(e) => {
                                            e.preventDefault();
                                            e.stopPropagation();
                                            requestCalc({ type: 'one', brandKey, product: p, key: p.url });
                                          }}
                                        >
                                          <Truck size={14} aria-hidden="true" /> Calcular Frete
                                        </button>
                                      </div>
                                    </div>
                                  );
                                }

                                // Calculado, com opções de entrega �  resumo colapsável
                                if (hasOptions) {
                                  const cheapest = opts[0];
                                  const cheapestFree = cheapest.is_free_shipping === true || cheapest.price === 0 || cheapest.price === 0.0;
                                  const summary = cheapestFree
                                    ? 'Frete Grátis'
                                    : `Frete a partir de R$ ${(cheapest.price ?? 0).toFixed(2).replace('.', ',')}`;
                                  return (
                                    <div className="shipping-section">
                                      <button
                                        type="button"
                                        className="shipping-summary"
                                        aria-expanded={isExpanded}
                                        onClick={(e) => {
                                          e.preventDefault();
                                          e.stopPropagation();
                                          setExpandedShipping(prev => ({ ...prev, [p.url]: !prev[p.url] }));
                                        }}
                                      >
                                        <span className="shipping-summary-main">
                                          <Truck size={14} aria-hidden="true" />
                                          <span className={cheapestFree ? 'shipping-free' : undefined}>{summary}</span>
                                        </span>
                                        {isExpanded ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                                      </button>
                                      {isExpanded && (
                                        <ul className="shipping-options-list">
                                          {/* Backend order: price asc then estimate asc � do NOT re-sort (D-10) */}
                                          {opts.map((opt: any, idx: number) => {
                                            const isFree = opt.is_free_shipping === true || opt.price === 0 || opt.price === 0.0;
                                            const serviceName = opt.service_name || opt.service_id || 'Entrega';
                                            const estimateText = opt.estimate_display || opt.raw_text || (opt.estimated_delivery_days ? `Até ${opt.estimated_delivery_days} dias úteis` : '');
                                            return (
                                              <li key={idx} className="shipping-option-row">
                                                <div className="shipping-option-service">
                                                  <span className="shipping-service-name">{serviceName}</span>
                                                  {estimateText && <span className="shipping-estimate">{estimateText}</span>}
                                                </div>
                                                <div className="shipping-option-price">
                                                  {isFree ? (
                                                    <span className="shipping-free">
                                                      <CheckCircle2 size={12} aria-hidden="true" />
                                                      Frete Grátis
                                                    </span>
                                                  ) : (
                                                    <span className="shipping-paid">
                                                      R$ {(opt.price ?? 0).toFixed(2).replace('.', ',')}
                                                    </span>
                                                  )}
                                                </div>
                                              </li>
                                            );
                                          })}
                                        </ul>
                                      )}
                                    </div>
                                  );
                                }

                                // Calculado sem opções �  estado indisponível / falha temporária
                                const statusLower = (p.shipping?.status || '').toLowerCase();
                                const isFailure = p._shipping_state === 'temporary_failure' || statusLower.includes('temporariamente');
                                const isUnavailable = p._shipping_state === 'unavailable_for_cep' || statusLower.includes('indisponível');
                                if (isFailure || isUnavailable) {
                                  const stateText = isFailure
                                    ? 'Frete temporariamente indisponível'
                                    : 'Entrega indisponível para este CEP';
                                  return (
                                    <div className="shipping-section">
                                      <div className={`shipping-state-row ${isFailure ? 'shipping-state-warning' : 'shipping-state-unavailable'}`}>
                                        {isFailure ? <AlertTriangle size={13} aria-hidden="true" /> : <MapPin size={13} aria-hidden="true" />}
                                        <span>{stateText}</span>
                                        {isFailure && (isBrandShippingSupported || (isVtex && p.sku_id)) && (
                                          <button
                                            type="button"
                                            className="shipping-retry"
                                            onClick={(e) => {
                                              e.preventDefault();
                                              e.stopPropagation();
                                              requestCalc({ type: 'one', brandKey, product: p, key: p.url });
                                            }}
                                          >
                                            Tentar novamente
                                          </button>
                                        )}
                                      </div>
                                    </div>
                                  );
                                }

                                // Fallback legado: registros antigos com p.shipping simples (D-08 compat)
                                if (p.shipping) {
                                  return (
                                    <>
                                      <div className={`product-meta product-shipping-legacy${p.shipping.status === 'Grátis' ? ' is-free' : ''}`}>
                                        <Package size={14} aria-hidden="true" />
                                        <span>
                                          {p.shipping.status === 'Grátis' ? 'Frete Grátis' : (p.shipping.price ? `Frete: R$ ${p.shipping.price.toFixed(2)}` : p.shipping.status)}
                                          {p.shipping.estimated_delivery_days ? ` (${p.shipping.estimated_delivery_days} dias)` : ''}
                                        </span>
                                      </div>
                                    </>
                                  );
                                }

                                // Nenhum estado de frete conhecido (ex.: engine sem provedor, tipo SFCC) � 
                                // Matriz Regional continua visível (D-07); o modal comunica "unsupported".
                                return <div className="shipping-section"></div>;
                              })()}
                            </div>
                            <div className="product-card-actions">
                              <button
                                type="button"
                                className="btn-icon btn-sm"
                                title="Adicionar ao monitoramento"
                                onClick={async (e) => {
                                  e.preventDefault();
                                  e.stopPropagation();
                                  await handleAddToMonitor(p.url, "unknown");
                                }}
                              >
                                <Plus size={14} />
                              </button>
                            </div>
                          </div>
                        ))}
                        {products.length === 0 && !brandRes?.error && (
                          <EmptyState title="Nenhum resultado nesta marca" description="A busca foi concluída, mas não retornou produtos comparáveis." />
                        )}
                      </div>
                    </div>
                  );
                })}
              </>
            );
          })()}
        </div>
        )}

        {/* Modal de CEP � pede o CEP quando o usuário calcula frete sem ter informado um */}
      </TableRegion>

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
              onKeyDown={(e) => { if (e.key === 'Enter') confirmCep(); }}
            />
          </div>
          {cepError && (
            <p className="cep-helper cep-helper-error cep-modal-error" role="alert" aria-live="polite">
              <AlertTriangle size={12} aria-hidden="true" /> {cepError}
            </p>
          )}
          <div className="dialog-actions dialog-actions--equal">
            <Button variant="outline" onClick={() => setCepModalOpen(false)}>Cancelar</Button>
            <Button onClick={confirmCep}>Calcular</Button>
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

