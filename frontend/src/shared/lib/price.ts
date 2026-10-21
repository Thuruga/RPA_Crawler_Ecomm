/**
 * Utilitários de preço e ofertas condicionais.
 * Extraído do App.tsx monolítico durante reorganização DDD.
 */
import type { ConditionalOffer } from '../../api/client';

export const AUTO_SWEEP_POLL_MS = 5000;
export const AUTO_SWEEP_MAX_ATTEMPTS = 20;

export const toPositivePrice = (value: unknown): number | null => {
  const num = typeof value === 'string' ? parseFloat(value) : (typeof value === 'number' ? value : NaN);
  return Number.isFinite(num) && num > 0 ? num : null;
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const getProductPriceView = (product: any) => {
  const priceFull = toPositivePrice(product?.price_full);
  const priceDiscount = toPositivePrice(product?.price_discount);
  const isDelta = product?.price_discount_is_delta === true;

  if (priceFull == null) {
    return { current: null, original: null, discountPct: null };
  }

  if (priceDiscount == null) {
    return { current: priceFull, original: null, discountPct: null };
  }

  if (isDelta) {
    const original = priceFull + priceDiscount;
    return {
      current: priceFull,
      original: original > priceFull ? original : null,
      discountPct: original > priceFull
        ? Math.round(((original - priceFull) / original) * 100)
        : null,
    };
  }

  if (priceDiscount < priceFull) {
    return {
      current: priceDiscount,
      original: priceFull,
      discountPct: Math.round(((priceFull - priceDiscount) / priceFull) * 100),
    };
  }

  return { current: priceFull, original: null, discountPct: null };
};

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const getMonitorPriceView = (monitor: any) => {
  const current = toPositivePrice(monitor?.last_price);
  const original = toPositivePrice(monitor?.last_price_original);
  if (current == null) {
    return { current: null, original: null };
  }
  if (original != null && original > current) {
    return { current, original };
  }

  const legacyDelta = toPositivePrice(monitor?.last_price_discount);
  if (legacyDelta != null && legacyDelta < current * 5) {
    return { current, original: current + legacyDelta };
  }

  return { current, original: null };
};

export const formatOfferPrice = (offer?: ConditionalOffer | null) => {
  const value = offer?.effective_unit_price ?? offer?.price;
  return typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat('pt-BR', { style: 'currency', currency: offer?.currency || 'BRL' }).format(value)
    : null;
};

export const formatOfferDiscount = (offer: ConditionalOffer) => {
  if (offer.discount_value == null) return null;
  if (offer.discount_unit === '%') return `${offer.discount_value}%`;
  if (offer.discount_unit === 'amount') {
    return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: offer.currency || 'BRL' }).format(offer.discount_value);
  }
  return `${offer.discount_value}`;
};

export const offerStateMeta = (state?: ConditionalOffer['state']) => {
  if (state === 'confirmed') return { label: 'Confirmada', tone: 'success' as const };
  if (state === 'requires_auth') return { label: 'Requer login', tone: 'warning' as const };
  if (state === 'expired') return { label: 'Encerrada', tone: 'info' as const };
  return { label: 'Anunciada', tone: 'info' as const };
};

export const offerResultMessage = (status?: string) => {
  if (status === 'none_detected') return 'Nenhuma oferta condicional foi detectada nesta verificacao.';
  if (status === 'unsupported') return 'A verificacao de ofertas nao e compativel com esta loja.';
  if (status === 'blocked') return 'A loja bloqueou a verificacao. Isso nao confirma ausencia de ofertas.';
  if (status === 'requires_auth') return 'A loja exige login para revelar as condicoes ou o valor.';
  if (status === 'error') return 'Nao foi possivel verificar as ofertas agora.';
  return null;
};
