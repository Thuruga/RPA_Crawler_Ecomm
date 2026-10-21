import { useState, useEffect, useRef, useMemo } from 'react';
import {
  RefreshCw, Layers, Radar, FileSpreadsheet, Download,
  ArrowUpDown,
  ExternalLink, Search,
  Package, LayoutDashboard
} from 'lucide-react';
import { toast } from 'sonner';
import { ApiClient, type CategoryProductItem, type BrandAveragesMap } from '../../api/client';
import { getClerkToken } from '../../api/clerkTokenBridge';
import { Panel } from '../../components/ui/Panel';
import { Button, IconButton } from '../../components/ui/Button';
import { EmptyState } from '../../components/ui/Feedback';
import { ProductDetailModal } from '../product/ProductDetailModal';
import { brandDisplayName, isMarketplaceBrand, type BrandItem } from '../../shared/lib';

interface CategoryOption {
  slug: string;
  label: string;
}

export type PriceSortOrder = 'default' | 'desc' | 'asc';

interface CategoryApiGroupItem {
  path: string;
  label?: string;
  name?: string;
}

interface CategoryApiGroup {
  group?: string;
  name?: string;
  items?: CategoryApiGroupItem[];
  categories?: CategoryApiGroupItem[];
}

interface BrandCategoriesResponse {
  categories?: CategoryApiGroup[];
}

interface CategoryWsMessage {
  type?: string;
  brand?: string;
  brand_name?: string;
  url?: string;
  product?: {
    product_name?: string;
    price?: number;
    price_original?: number | null;
    url?: string;
    image_url?: string | null;
    sku?: string | null;
    brand?: string;
    is_available?: boolean;
  };
  brand_averages?: BrandAveragesMap;
  output_file?: string;
  progress?: {
    current: number;
    total: number;
    success: number;
    error: number;
  };
  message?: string;
}

const cleanCategoryLabel = (rawLabel: string): string => {
  return rawLabel
    .replace(/^(Mapeadas Manualmente|Categorias Principais|Categorias)\s*[-–:]\s*/i, '')
    .replace(/^\[(Tabela|Canônico|Canonico)\]\s*/i, '')
    .trim();
};

export const CategoryPage = ({ brands }: { brands: BrandItem[] }) => {
  const [selectedBrands, setSelectedBrands] = useState<string[]>([]);
  const [brandCategories, setBrandCategories] = useState<Record<string, CategoryOption[]>>({});
  const [brandSelections, setBrandSelections] = useState<Record<string, string>>({});
  const [isScraping, setIsScraping] = useState(false);
  const [triggeringScan, setTriggeringScan] = useState(false);
  const [triggeringSingleScan, setTriggeringSingleScan] = useState<Record<string, boolean>>({});
  const [zipcode, setZipcode] = useState('');

  // Streaming & Dual View states
  const [extractedProducts, setExtractedProducts] = useState<CategoryProductItem[]>([]);
  const [searchFilter, setSearchFilter] = useState('');
  const [priceSort, setPriceSort] = useState<PriceSortOrder>('default');
  const [isExporting, setIsExporting] = useState(false);
  const [brandAverages, setBrandAverages] = useState<BrandAveragesMap>({});
  const [detailModal, setDetailModal] = useState<{ brand: string; url: string; seed?: CategoryProductItem } | null>(null);

  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.onmessage = null;
        wsRef.current.onopen = null;
        wsRef.current.onerror = null;
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, []);

  const formatCurrency = (val: number | null | undefined) => {
    if (val == null || isNaN(val)) return '—';
    return `R$ ${val.toFixed(2).replace('.', ',')}`;
  };

  const handleAddToMonitor = async (product: CategoryProductItem) => {
    if (!product.url) return;
    try {
      const brandObj = brands.find(b =>
        b.brand_name?.toLowerCase() === product.brand?.toLowerCase() ||
        b.brand_key?.toLowerCase() === product.brand?.toLowerCase()
      );
      const brandKey = brandObj?.brand_key || product.brand?.toLowerCase();
      const result = await ApiClient.addToMonitor(product.url, brandKey);
      if (result.status === 'already_active') {
        toast.info('Produto já está em monitoramento');
      } else if (result.status === 'reactivated') {
        toast.success('Monitor reativado');
      } else {
        toast.success(`Adicionado ao monitoramento (${product.product_name || product.brand})`);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error('Erro ao adicionar ao monitoramento: ' + message);
    }
  };

  const fetchBrandCategories = async (brandKey: string) => {
    if (brandCategories[brandKey]) return;
    try {
      const data = await ApiClient.request<BrandCategoriesResponse>(`/brands/${brandKey}/categories`);
      if (data && data.categories) {
        const seen = new Set<string>();
        const flat: CategoryOption[] = [];

        for (const group of data.categories) {
          const rawItems = group.items || group.categories || [];
          if (!Array.isArray(rawItems)) continue;
          const groupName = (group.group || group.name || '').trim();
          const isGenericGroup = !groupName || /^(categorias|categorias principais|mapeadas manualmente)$/i.test(groupName);

          for (const i of rawItems) {
            const rawLabel = (i.label || i.name || i.path || '').trim();
            const clean = cleanCategoryLabel(rawLabel);
            const labelAlreadyNamesGroup = clean.toLocaleLowerCase('pt-BR')
              .includes(groupName.toLocaleLowerCase('pt-BR'));
            const displayLabel = isGenericGroup || labelAlreadyNamesGroup
              ? clean
              : `${groupName} - ${clean}`;
            const key = `${groupName.toLowerCase()}|${i.path.toLowerCase()}`;
            if (!seen.has(key)) {
              seen.add(key);
              flat.push({
                slug: i.path,
                label: displayLabel,
              });
            }
          }
        }

        flat.sort((a, b) => {
          const aHasParen = a.label.includes('(');
          const bHasParen = b.label.includes('(');
          if (aHasParen !== bHasParen) return aHasParen ? 1 : -1;
          return a.label.localeCompare(b.label, 'pt-BR');
        });

        setBrandCategories(prev => ({ ...prev, [brandKey]: flat }));
      }
    } catch (err: unknown) {
      console.error(`Erro ao carregar categorias para ${brandKey}:`, err);
    }
  };

  const toggleBrand = (key: string) => {
    setSelectedBrands(prev => {
      const exists = prev.includes(key);
      if (exists) {
        const next = prev.filter(k => k !== key);
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { [key]: _, ...rest } = brandSelections;
        setBrandSelections(rest);
        return next;
      } else {
        fetchBrandCategories(key);
        return [...prev, key];
      }
    });
  };

  const updateBrandSelection = (brandKey: string, categoryPath: string) => {
    setBrandSelections(prev => ({ ...prev, [brandKey]: categoryPath }));
  };

  const handleTriggerBackgroundScan = async () => {
    if (selectedBrands.length === 0) return;
    setTriggeringScan(true);
    let totalEnqueued = 0;
    try {
      for (const bk of selectedBrands) {
        const res = await ApiClient.triggerCategoryScan(bk);
        if (res.enqueued && res.enqueued.length > 0) {
          totalEnqueued += res.enqueued.length;
        }
      }
      toast.success(`Monitoramento enfileirado para ${totalEnqueued} categorias em background.`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Erro ao disparar monitoramento: ${message}`);
    } finally {
      setTriggeringScan(false);
    }
  };

  const handleTriggerSingleScan = async (brandKey: string) => {
    setTriggeringSingleScan(prev => ({ ...prev, [brandKey]: true }));
    try {
      const res = await ApiClient.triggerCategoryScan(brandKey);
      toast.success(`Monitoramento enfileirado para ${res.enqueued.length} categoria(s).`);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Erro ao disparar monitoramento: ${message}`);
    } finally {
      setTriggeringSingleScan(prev => ({ ...prev, [brandKey]: false }));
    }
  };

  const startScrape = async () => {
    if (selectedBrands.length === 0) return;

    const allSelected = selectedBrands.every(bk => brandSelections[bk]);
    if (!allSelected) {
      toast.error('Por favor, selecione uma categoria para cada marca.');
      return;
    }
    const cep = zipcode.replace(/\D/g, '');
    if (cep && cep.length !== 8) {
      toast.error('Informe um CEP válido com 8 dígitos.');
      return;
    }

    setIsScraping(true);
    setExtractedProducts([]);
    setBrandAverages({});
    setSearchFilter('');

    try {
      const isMulti = selectedBrands.length > 1;
      let payload: Record<string, unknown>;

      if (isMulti) {
        payload = {
          brands: selectedBrands,
          brand_category_map: brandSelections,
          category_slug: "Varredura Manual",
          zipcode: cep || undefined,
        };
      } else {
        const brand = selectedBrands[0];
        const selection = brandSelections[brand];
        if (selection.startsWith('http')) {
          payload = { brand, custom_url: selection, zipcode: cep || undefined };
        } else {
          payload = { brand, category_path: selection, zipcode: cep || undefined };
        }
      }

      const res = await ApiClient.startScrape(payload, isMulti);
      const jobId = res.job_id;

      const token = await getClerkToken();
      const authQuery = token ? `?token=${encodeURIComponent(token)}` : '';
      const apiUrl = import.meta.env.VITE_API_URL || '';
      let wsUrl: string;
      if (apiUrl) {
        wsUrl = apiUrl.replace(/^http/, 'ws') + `/ws/${jobId}${authQuery}`;
      } else {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        wsUrl = `${protocol}//${window.location.host}/ws/${jobId}${authQuery}`;
      }
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onmessage = (e) => {
        let msg: CategoryWsMessage;
        try {
          msg = JSON.parse(e.data) as CategoryWsMessage;
        } catch {
          return;
        }

        if (msg.type === 'category_product_item' && msg.product) {
          const item: CategoryProductItem = {
            ...msg.product,
            product_name: msg.product.product_name || 'Produto',
            price: typeof msg.product.price === 'number' ? msg.product.price : 0,
            price_original: msg.product.price_original ?? null,
            url: msg.product.url || '',
            image_url: msg.product.image_url ?? null,
            sku: msg.product.sku ?? null,
            brand: msg.product.brand || msg.brand_name || msg.brand || '',
            is_available: msg.product.is_available ?? true,
          };

          setExtractedProducts(prev => {
            if (item.url && prev.some(existing => existing.url === item.url)) {
              return prev;
            }
            return [...prev, item];
          });

          const bKey = (msg.brand || item.brand || '').toLowerCase();
          if (bKey && typeof item.price === 'number' && item.price > 0) {
            setBrandAverages(prev => {
              const existing = prev[bKey] || { average_price: null, average_price_original: null, count: 0 };
              const newCount = existing.count + 1;
              const currentAvg = existing.average_price ?? 0;
              const newAvg = Number(((currentAvg * existing.count + item.price) / newCount).toFixed(2));
              return {
                ...prev,
                [bKey]: {
                  ...existing,
                  count: newCount,
                  average_price: newAvg,
                }
              };
            });
          }
        } else if (msg.type === 'done' || msg.type === 'cancelled_done' || msg.type === 'error_done') {
          setIsScraping(false);
          ws.close();
          if (msg.brand_averages && Object.keys(msg.brand_averages).length > 0) {
            setBrandAverages(msg.brand_averages);
          }
        }
      };
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Erro na varredura: ${message}`);
      setIsScraping(false);
    }
  };

  const filteredProducts = extractedProducts.filter(p => {
    if (!searchFilter.trim()) return true;
    const term = searchFilter.toLowerCase();
    const nameMatch = (p.product_name || '').toLowerCase().includes(term);
    const brandMatch = (p.brand || '').toLowerCase().includes(term);
    return nameMatch || brandMatch;
  });

  const sortedAndFilteredProducts = useMemo(() => {
    if (priceSort === 'default') return filteredProducts;
    return [...filteredProducts].sort((a, b) => {
      const pa = typeof a.price === 'number' && !isNaN(a.price) ? a.price : null;
      const pb = typeof b.price === 'number' && !isNaN(b.price) ? b.price : null;
      if (pa === null && pb === null) return 0;
      if (pa === null) return 1;
      if (pb === null) return -1;
      return priceSort === 'desc' ? pb - pa : pa - pb;
    });
  }, [filteredProducts, priceSort]);

  const handleExportExcel = async () => {
    if (sortedAndFilteredProducts.length === 0) {
      toast.info('Nenhum produto para exportar.');
      return;
    }
    setIsExporting(true);
    try {
      await ApiClient.exportCategoryProducts({
        items: sortedAndFilteredProducts,
        filename: `produtos_categoria_${new Date().toISOString().slice(0, 10)}.xlsx`,
      });
      toast.success('Download do Excel iniciado com sucesso!');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Erro ao baixar Excel: ${message}`);
    } finally {
      setIsExporting(false);
    }
  };

  const activeBrands = (() => {
    if (selectedBrands.length > 1) {
      return selectedBrands;
    }
    const distinctInProducts = Array.from(
      new Set(filteredProducts.map(p => p.brand).filter(Boolean))
    );
    if (distinctInProducts.length > 1) {
      return distinctInProducts;
    }
    return selectedBrands.length > 0 ? selectedBrands : distinctInProducts;
  })();

  const isMultiBrand = activeBrands.length > 1;

  const getBrandProducts = (brandKeyOrName: string) => {
    const target = brandKeyOrName.trim().toLowerCase();
    const brandObj = brands.find(b =>
      b.brand_key?.toLowerCase() === target ||
      b.brand_name?.toLowerCase() === target
    );
    const targetKey = brandObj?.brand_key?.toLowerCase() || target;
    const targetName = brandObj?.brand_name?.toLowerCase() || target;

    return sortedAndFilteredProducts.filter(p => {
      const pBrand = (p.brand || '').trim().toLowerCase();
      return pBrand === targetKey || pBrand === targetName;
    });
  };

  const renderProductCard = (p: CategoryProductItem, idx: number) => {
    const hasDiscount = p.price_original != null && p.price_original > p.price;
    const discountPct = hasDiscount
      ? Math.round(((p.price_original! - p.price) / p.price_original!) * 100)
      : 0;
    const brandName = brandDisplayName(p.brand, brands) || p.brand;

    return (
      <div
        key={`${p.url || p.product_name}-${idx}`}
        className={`monitor-item ${p.url ? 'is-interactive' : ''}`}
        data-testid="product-card"
        role={p.url ? 'button' : undefined}
        tabIndex={p.url ? 0 : undefined}
        aria-label={p.url ? `Abrir detalhes de ${p.product_name || 'produto'}` : undefined}
        onClick={() => p.url && setDetailModal({ brand: p.brand, url: p.url, seed: p })}
        onKeyDown={p.url ? (event) => {
          if (event.target !== event.currentTarget) return;
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            setDetailModal({ brand: p.brand, url: p.url, seed: p });
          }
        } : undefined}
      >
        <div className="monitor-image-small">
          {p.image_url ? (
            <img
              src={p.image_url}
              alt={p.product_name}
              loading="lazy"
              onError={(e: React.SyntheticEvent<HTMLImageElement>) => {
                e.currentTarget.style.display = 'none';
              }}
            />
          ) : (
            <Package size={22} className="text-muted" />
          )}
        </div>

        <div className="monitor-info">
          <div className="monitor-main">
            <span className="brand-badge-small">{brandName}</span>
          </div>
          <p className="monitor-product-name" title={p.product_name}>
            {p.product_name}
          </p>
          {p.url && (
            <span className="monitor-url" title={p.url}>
              {p.url}
            </span>
          )}
        </div>

        <div className="monitor-pricing">
          {hasDiscount && (
            <span className="price-original">{formatCurrency(p.price_original)}</span>
          )}
          <span className="monitor-price-value">{formatCurrency(p.price)}</span>
          {p.is_available === false ? (
            <span className="status-pill">Esgotado</span>
          ) : hasDiscount ? (
            <span className="status-pill status-pill--active">-{discountPct}%</span>
          ) : null}
        </div>

        <div className="monitor-actions">
          <button
            type="button"
            className="icon-button icon-button--outline"
            aria-label="Adicionar ao monitoramento"
            title="Adicionar ao monitoramento"
            onClick={(e) => {
              e.stopPropagation();
              handleAddToMonitor(p);
            }}
          >
            <LayoutDashboard size={15} />
          </button>
          {p.url && (
            <a
              href={p.url}
              target="_blank"
              rel="noopener noreferrer"
              className="icon-button icon-button--outline"
              title="Abrir no site"
              aria-label="Abrir no site"
              onClick={(e) => e.stopPropagation()}
            >
              <ExternalLink size={15} />
            </a>
          )}
        </div>
      </div>
    );
  };

  const hasBrandAverages = Object.keys(brandAverages).length > 0;

  return (
    <div className="page-content">
      <div className="grid-category">
        <div className="category-sidebar">
          <Panel
            className="glass-card"
            title="Configuração da Varredura"
            action={
              <button
                type="button"
                className="btn-text text-muted"
                onClick={handleTriggerBackgroundScan}
                disabled={triggeringScan || selectedBrands.length === 0 || isScraping}
                title="Atualiza todos os monitores em background das marcas selecionadas"
                style={{ fontSize: '0.85rem', display: 'flex', alignItems: 'center', gap: '6px', background: 'transparent', border: 'none', cursor: 'pointer' }}
              >
                <RefreshCw size={14} className={triggeringScan ? 'animate-spin' : ''} />
                Atualizar categorias
              </button>
            }
          >
            <div className="form-stack">
              <div className="form-group">
                <label className="label">Marcas Alvo</label>
                <div className="brand-selector-grid category-brand-grid">
                  {brands
                    .filter(b => !isMarketplaceBrand(b))
                    .map(b => (
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
                            onError={(e: React.SyntheticEvent<HTMLImageElement>) => { e.currentTarget.src = `https://ui-avatars.com/api/?name=${encodeURIComponent(b.brand_name)}&background=000000&color=ffffff`; }}
                          />
                        </div>
                        <span>{b.brand_name}</span>
                      </button>
                    ))}
                </div>
              </div>

              <div className="form-group">
                <label className="label">Categorias por Marca</label>
                {selectedBrands.length === 0 && (
                  <EmptyState
                    title="Nenhuma marca selecionada"
                    description="Selecione uma ou mais marcas acima para configurar as categorias."
                    icon={<Layers size={20} aria-hidden="true" />}
                  />
                )}

                <div className="brand-category-selectors">
                  {selectedBrands.map(bk => {
                    const brand = brands.find(b => b.brand_key === bk);
                    return (
                      <div key={bk} className="brand-category-item">
                        <div className="brand-category-item__header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <img
                              src={brand?.logo_url || `https://www.google.com/s2/favicons?domain=${brand?.domain}&sz=32`}
                              className="brand-category-item__logo"
                              alt={brand?.brand_name || 'Marca'}
                            />
                            <span>{brand?.brand_name}</span>
                          </div>
                          <IconButton
                            label={`Atualizar categorias da marca ${brand?.brand_name}`}
                            onClick={() => handleTriggerSingleScan(bk)}
                            disabled={triggeringSingleScan[bk]}
                            title={`Atualizar categorias da marca ${brand?.brand_name}`}
                          >
                            <RefreshCw size={16} className={triggeringSingleScan[bk] ? 'animate-spin' : ''} />
                          </IconButton>
                        </div>
                        <select
                          className="input"
                          aria-label={`Categoria para ${brand?.brand_name || bk}`}
                          value={brandSelections[bk] || ""}
                          onChange={e => updateBrandSelection(bk, e.target.value)}
                        >
                          <option value="">Selecione a categoria...</option>
                          {(brandCategories[bk] || []).map((cat: CategoryOption, index: number) => (
                            <option key={`${cat.slug}-${index}`} value={cat.slug}>{cat.label}</option>
                          ))}
                        </select>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="mt-4" style={{ display: 'flex', gap: '12px', alignItems: 'end' }}>
                <Button className="w-full" onClick={startScrape} disabled={isScraping || selectedBrands.length === 0}
                  loading={isScraping} loadingLabel="Processando varredura" icon={<Layers size={18} aria-hidden="true" />}>
                  Iniciar varredura
                </Button>
                <div className="form-group" style={{ margin: 0, flex: '0 0 170px' }}>
                  <label className="label" htmlFor="category-zipcode">CEP para frete</label>
                  <input id="category-zipcode" className="input" inputMode="numeric" placeholder="00000-000" value={zipcode}
                    onChange={e => { const d = e.target.value.replace(/\D/g, '').slice(0, 8); setZipcode(d.length > 5 ? `${d.slice(0, 5)}-${d.slice(5)}` : d); }} />
                </div>
              </div>
            </div>
          </Panel>
        </div>

        <div className="category-main">
          {/* Top Bar: Média por Marca (D-12) */}
          {(hasBrandAverages || extractedProducts.length > 0) && (
            <div className="category-metrics-panel" data-testid="category-metrics-panel">
              <div className="category-metrics-header">
                <div className="category-metrics-title">
                  <span className="category-metrics-icon" aria-hidden="true">
                    <Radar size={16} />
                  </span>
                  <span>Média por Marca</span>
                </div>
                <span className="category-metrics-counter">
                  {extractedProducts.length} produto(s) extraído(s)
                </span>
              </div>
              <div className="category-metrics-chips">
                {hasBrandAverages ? (
                  Object.entries(brandAverages).map(([bKey, avg]) => {
                    const brandObj = brands.find(b =>
                      b.brand_key?.toLowerCase() === bKey.toLowerCase() ||
                      b.brand_name?.toLowerCase() === bKey.toLowerCase()
                    );
                    const name = brandObj?.brand_name || bKey;
                    const logoUrl = brandObj?.logo_url || (brandObj?.domain ? `https://www.google.com/s2/favicons?domain=${brandObj.domain}&sz=64` : undefined);
                    return (
                      <div key={bKey} className="category-brand-chip" data-testid={`brand-chip-${bKey}`}>
                        {logoUrl && (
                          <div className="category-brand-chip__icon" aria-hidden="true">
                            <img
                              src={logoUrl}
                              alt=""
                              onError={(e: React.SyntheticEvent<HTMLImageElement>) => {
                                const parent = e.currentTarget.parentElement;
                                if (parent) parent.style.display = 'none';
                              }}
                            />
                          </div>
                        )}
                        <span className="category-brand-chip__name">{name}:</span>
                        <span className="category-brand-chip__price">{formatCurrency(avg.average_price)}</span>
                        <span className="category-brand-chip__count">{avg.count} un</span>
                      </div>
                    );
                  })
                ) : (
                  <span className="category-metrics-empty">Calculando médias por marca...</span>
                )}
              </div>
            </div>
          )}

          {/* Produtos Extraídos */}
          <Panel
            className="glass-card mb-4"
            title="Produtos Extraídos"
            subtitle={isScraping ? "Atualizando em tempo real..." : `${sortedAndFilteredProducts.length} itens extraídos${sortedAndFilteredProducts.length !== extractedProducts.length ? ` (de ${extractedProducts.length})` : ''}`}
          >
            {extractedProducts.length > 0 && (
              <div className="category-view-toolbar">
                <div className="category-search-box">
                  <Search size={16} className="category-search-icon" />
                  <input
                    type="text"
                    placeholder="Filtrar entre os produtos extraídos..."
                    value={searchFilter}
                    onChange={e => setSearchFilter(e.target.value)}
                    aria-label="Filtrar produtos extraídos"
                  />
                </div>

                <div className="category-sort-box">
                  <ArrowUpDown size={14} className="category-sort-icon" />
                  <select
                    className="input category-sort-select"
                    value={priceSort}
                    onChange={e => setPriceSort(e.target.value as PriceSortOrder)}
                    aria-label="Ordenar produtos por preço"
                  >
                    <option value="default">Ordem padrão</option>
                    <option value="desc">Maior preço → Menor preço</option>
                    <option value="asc">Menor preço → Maior preço</option>
                  </select>
                </div>

                <button
                  type="button"
                  className="btn btn-excel category-export-btn"
                  onClick={handleExportExcel}
                  disabled={isExporting || sortedAndFilteredProducts.length === 0}
                  title="Baixar resultado dos produtos em Excel"
                  aria-label="Baixar resultado no Excel"
                >
                  {isExporting ? (
                    <RefreshCw size={16} className="animate-spin" />
                  ) : (
                    <FileSpreadsheet size={16} />
                  )}
                  <span>{isExporting ? "Baixando..." : "Baixar Excel"}</span>
                  <Download size={14} aria-hidden="true" />
                </button>
              </div>
            )}

            {filteredProducts.length === 0 ? (
              extractedProducts.length === 0 ? (
                <EmptyState
                  title="Nenhum produto extraído ainda"
                  description="Selecione as categorias desejadas e clique em 'Iniciar Varredura' para acompanhar os produtos em tempo real."
                  icon={<Layers size={24} aria-hidden="true" />}
                />
              ) : (
                <EmptyState
                  title="Nenhum produto encontrado"
                  description={`Nenhum produto corresponde ao filtro "${searchFilter}".`}
                  icon={<Search size={24} aria-hidden="true" />}
                />
              )
            ) : isMultiBrand ? (
              <div className="category-columns-container results-container" data-testid="category-product-grid" style={{ gridTemplateColumns: activeBrands.length > 0 ? `repeat(${activeBrands.length}, minmax(0, 1fr))` : undefined }}>
                {activeBrands.map((brandKey) => {
                  const brandObj = brands.find(b =>
                    b.brand_key?.toLowerCase() === brandKey.toLowerCase() ||
                    b.brand_name?.toLowerCase() === brandKey.toLowerCase()
                  );
                  const brandName = brandObj?.brand_name || brandDisplayName(brandKey, brands) || brandKey;
                  const brandProducts = getBrandProducts(brandKey);

                  return (
                    <div key={brandKey} className="brand-column category-brand-column" data-testid={`brand-column-${brandKey}`}>
                      <div className="brand-header category-brand-header">
                        <div className="category-brand-header__title">
                          {brandObj?.logo_url && (
                            <img
                              src={brandObj.logo_url}
                              alt={brandName}
                              className="category-brand-header__logo"
                              onError={(e: React.SyntheticEvent<HTMLImageElement>) => { e.currentTarget.style.display = 'none'; }}
                            />
                          )}
                          <span>{brandName}</span>
                        </div>
                        <span className="category-brand-header__count">{brandProducts.length} itens</span>
                      </div>

                      <div className="monitor-list">
                        {brandProducts.length === 0 ? (
                          <div className="category-column-empty">
                            {isScraping ? 'Aguardando produtos...' : 'Nenhum produto para esta marca'}
                          </div>
                        ) : (
                          brandProducts.map((p, idx) => renderProductCard(p, idx))
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="monitor-list category-product-grid" data-testid="category-product-grid">
                {sortedAndFilteredProducts.map((p, idx) => renderProductCard(p, idx))}
              </div>
            )}
          </Panel>

        </div>
      </div>

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
