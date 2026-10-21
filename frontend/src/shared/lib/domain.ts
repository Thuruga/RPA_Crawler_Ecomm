/**
 * Utilitários de domínio/marca.
 * Extraído do App.tsx monolítico durante reorganização DDD.
 */

export const SKU_PATTERN = /^[A-Za-z]{2}\.\d{2}\.\d{7}$/;
export const SKU_ERROR_MSG = 'Formato inválido. Use o padrão AA.NN.NNNNNNN (ex: ML.05.0326046 ou CD.08.0292007).';

// Normaliza um domínio para comparação: minúsculas + remove ponto final (FQDN)
// + remove prefixo literal "www." (slice literal, NÃO lstrip — lstrip removeria o
// char-set {w,.} e corromperia hosts).
export const normalizeDomain = (domain: string): string => {
  const host = (domain || '').trim().toLowerCase().replace(/\.+$/, '');
  return host.startsWith('www.') ? host.slice('www.'.length) : host;
};

// Casa o host de uma URL com o domínio cadastrado de uma marca.
// Match exato OU por sufixo de LABEL: o domínio cadastrado de marketplace é o
// registrável (ex.: "mercadolivre.com.br"), mas a URL do produto pode vir em
// qualquer subdomínio ("produto.mercadolivre.com.br", "lista.mercadolivre.com.br",
// "www.amazon.com.br", "m.netshoes.com.br"). Sem isto, o identify-first casava só
// por igualdade exata: "produto.mercadolivre.com.br" ≠ "mercadolivre.com.br" ⇒ o
// ML nunca criava monitor (bug monitor-marketplace-pendente Round 2, causa ML).
// Boundary de label (host === base || host.endsWith("." + base)) para NÃO casar
// "maliciousmercadolivre.com.br".
export const domainMatchesBrand = (urlDomain: string, brandDomain: string): boolean => {
  const host = normalizeDomain(urlDomain);
  const base = normalizeDomain(brandDomain);
  if (!host || !base) return false;
  return host === base || host.endsWith(`.${base}`);
};

export type BrandItem = {
  brand_key: string;
  brand_name: string;
  domain: string;
  logo_url?: string;
  is_active?: boolean;
  engine?: string;
  is_marketplace?: boolean;
  [key: string]: unknown;
};

// Nome exibível de uma marca/marketplace: usa o brand_name cadastrado; sem cadastro,
// humaniza o brand_key ("mercado_livre" → "Mercado Livre").
export const brandDisplayName = (brandKey: string, brands: BrandItem[]): string => {
  const meta = brands.find(b => b.brand_key === brandKey);
  if (meta?.brand_name) return meta.brand_name;
  return (brandKey || '')
    .split('_')
    .filter(Boolean)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
};

export const MARKETPLACE_BRANDS = [
  'mercado_livre',
  'netshoes',
  'amazon',
  'dafiti',
  'privalia',
] as const;

export const isMarketplaceBrand = (brandOrKey?: string | BrandItem): boolean => {
  if (!brandOrKey) return false;
  if (typeof brandOrKey === 'object') {
    const b = brandOrKey;
    if (b.is_marketplace === true) return true;
    if (b.engine && MARKETPLACE_BRANDS.includes(b.engine as any)) return true;
    return isMarketplaceBrand(b.brand_key);
  }
  const raw = brandOrKey.trim().toLowerCase();
  const normalized = raw.replace(/[\s-]/g, '_');
  const collapsed = raw.replace(/[\s-_]/g, '');
  const keys: readonly string[] = MARKETPLACE_BRANDS;
  return keys.includes(raw as any) || keys.includes(normalized as any) || keys.some(k => k.replace(/[\s-_]/g, '') === collapsed);
};

// Ordena marcas: lojas próprias primeiro (A-Z), marketplaces depois (A-Z).
// Mantém os dois grupos separados para o usuário localizar mais rápido uma
// marca própria sem precisar rolar por Amazon/Mercado Livre/Netshoes/etc.
export const sortBrands = <T extends BrandItem>(brands: T[]): T[] => {
  const collator = new Intl.Collator('pt-BR', { sensitivity: 'base' });
  const byName = (a: T, b: T) => collator.compare(a.brand_name || a.brand_key, b.brand_name || b.brand_key);
  const stores = brands.filter(b => !isMarketplaceBrand(b)).sort(byName);
  const marketplaces = brands.filter(b => isMarketplaceBrand(b)).sort(byName);
  return [...stores, ...marketplaces];
};

