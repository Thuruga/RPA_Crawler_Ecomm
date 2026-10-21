import { useState, useEffect, Component, type ReactNode } from 'react';
import {
  Package,
  ExternalLink,
  RefreshCw,
  MessageSquare,
  Plus,
  Star,
} from 'lucide-react';
import { toast } from 'sonner';
import { Dialog } from '../../components/ui/Dialog';
import { Button } from '../../components/ui/Button';
import { Badge, Status, EmptyState } from '../../components/ui/Feedback';
import { Skeleton, SkeletonText, SkeletonRow } from '../../components/ui/Skeleton';
import {
  ApiClient,
  type ProductDetail,
  type ConditionalOffer,
  type ConditionalOfferEvent,
  type ConditionalOfferResult,
  type ReviewCommentsResult,
} from '../../api/client';
import {
  formatTimeGlobal,
  stripHtmlToText,
  formatOfferPrice,
  formatOfferDiscount,
  offerStateMeta,
  offerResultMessage,
  reviewsStateLabel,
  toPositivePrice,
} from '../../shared/lib';

const monitorEventLabel: Record<ConditionalOfferEvent['event_type'], string> = {
  started: 'Oferta iniciada',
  changed: 'Oferta alterada',
  state_changed: 'Estado alterado',
  ended: 'Oferta encerrada',
};

export const ConditionalOfferRow = ({ offer }: { offer: ConditionalOffer }) => {
  const state = offerStateMeta(offer.state);
  const value = formatOfferPrice(offer);
  const discount = formatOfferDiscount(offer);
  return (
    <li className="product-detail-offers__item">
      <div className="product-detail-offers__heading">
        <strong>{offer.condition_label}</strong>
        <Badge tone={state.tone}>{state.label}</Badge>
      </div>
      {value ? (
        <span className="product-detail-offers__price">{value}</span>
      ) : (
        <span className="text-muted">Valor não disponível</span>
      )}
      {offer.raw_text && <p className="product-detail-offers__copy">{offer.raw_text}</p>}
      {discount != null && <p className="product-detail-offers__copy">Desconto informado: {discount}</p>}
    </li>
  );
};

export type MonitorOfferInput = {
  conditional_offers_current?: ConditionalOffer[];
  conditional_offer_history?: ConditionalOfferEvent[];
  conditional_offers_diagnostics?: string[];
  conditional_offers_status?: string | null;
  [key: string]: unknown;
};

export const MonitorConditionalOffers = ({ monitor }: { monitor: MonitorOfferInput }) => {
  const current = (monitor.conditional_offers_current || []) as ConditionalOffer[];
  const history = (monitor.conditional_offer_history || []) as ConditionalOfferEvent[];
  const diagnostics = (monitor.conditional_offers_diagnostics || []) as string[];
  const degraded =
    monitor.conditional_offers_status === 'blocked' || monitor.conditional_offers_status === 'error';
  if (!current.length && !history.length && !degraded) return null;

  return (
    <section className="monitor-offer" aria-label="Ofertas condicionais do monitor">
      <div className="monitor-offer__current">
        <strong>Ofertas condicionais atuais</strong>
        {current.filter(offer => offer.state !== 'expired').length ? (
          <ul className="monitor-offer__list">
            {current
              .filter(offer => offer.state !== 'expired')
              .map(offer => (
                <ConditionalOfferRow
                  key={offer.offer_key || offer.condition_label}
                  offer={offer}
                />
              ))}
          </ul>
        ) : (
          <p className="text-muted text-xs">Nenhuma oferta condicional ativa.</p>
        )}
        {degraded && (
          <Status tone="warning">
            A última verificação foi {monitor.conditional_offers_status === 'blocked' ? 'bloqueada' : 'interrompida por erro'}; a última oferta conhecida foi preservada.
            {diagnostics[0] ? ` Diagnóstico: ${diagnostics[0]}` : ''}
          </Status>
        )}
      </div>
      {history.length > 0 && (
        <div className="monitor-offer__history">
          <strong>Histórico de ofertas condicionais</strong>
          <ol>
            {[...history].reverse().map((event, index) => {
              const offer = event.after || event.before;
              const before = formatOfferPrice(event.before);
              const after = formatOfferPrice(event.after);
              return (
                <li key={`${event.created_at}:${event.offer_key}:${index}`}>
                  <div className="monitor-offer__event-heading">
                    <span>{monitorEventLabel[event.event_type]}</span>
                    <time dateTime={event.created_at}>
                      {new Date(event.created_at).toLocaleString('pt-BR')}
                    </time>
                  </div>
                  <p>
                    {offer?.condition_label || 'Oferta condicional'} - {offerStateMeta(offer?.state).label}
                  </p>
                  {before !== after && (
                    <p>{before || 'Sem valor'} para {after || 'Sem valor'}</p>
                  )}
                  {(event.observed_from || event.observed_until) && (
                    <p>
                      Período: {event.observed_from ? new Date(event.observed_from).toLocaleString('pt-BR') : 'início desconhecido'} até {event.observed_until ? new Date(event.observed_until).toLocaleString('pt-BR') : 'em andamento'}
                    </p>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </section>
  );
};

export type ProductDetailSeed = {
  raw_title?: string;
  product_name?: string;
  image_url?: string | null;
  price_full?: number | string | null;
  price_discount?: number | string | null;
  stock_availability?: string;
  available?: boolean;
  conditional_offers_current?: ConditionalOffer[];
  conditional_offers?: ConditionalOffer[];
  conditional_offer_history?: ConditionalOfferEvent[];
  conditional_offers_status?: string | null;
  conditional_offers_diagnostics?: string[];
  [key: string]: unknown;
};

export type ProductDetailModalProps = {
  open: boolean;
  brand: string;
  url: string;
  seed?: ProductDetailSeed;
  /** Produtos de categoria podem ser promovidos ao monitor individual; itens já individuais não. */
  showMonitorAction?: boolean;
  onClose: () => void;
};

const ProductDetailModalContent = ({
  brand,
  url,
  seed,
  showMonitorAction = true,
  onClose,
}: Omit<ProductDetailModalProps, 'open'>) => {
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<ProductDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const seedOffers = (seed?.conditional_offers_current || seed?.conditional_offers || []) as ConditionalOffer[];
  const initialOffersResult: ConditionalOfferResult | null = seedOffers.length > 0 ? {
    status: ((seed?.conditional_offers_status as ConditionalOfferResult['status']) || 'found'),
    offers: seedOffers,
    checked_at: new Date().toISOString(),
    probe_level: 'full',
    sources_attempted: ['seed'],
    diagnostics: (seed?.conditional_offers_diagnostics as string[]) || [],
    cache: { hit: true, stale: false },
  } : null;

  const [offersLoading, setOffersLoading] = useState(!initialOffersResult);
  const [offersResult, setOffersResult] = useState<ConditionalOfferResult | null>(initialOffersResult);
  const [offersError, setOffersError] = useState<string | null>(null);
  const [offersAnnouncement, setOffersAnnouncement] = useState(
    initialOffersResult ? 'Ofertas carregadas.' : 'Verificando ofertas...'
  );
  const [reviewsLoading, setReviewsLoading] = useState(false);
  const [reviewsResult, setReviewsResult] = useState<ReviewCommentsResult | null>(null);
  const [reviewsError, setReviewsError] = useState<string | null>(null);
  const [showAllReviews, setShowAllReviews] = useState(false);
  const [addingToMonitor, setAddingToMonitor] = useState(false);

  useEffect(() => {
    const p = detail?.product as Record<string, any> | undefined;
    if (p) {
      if (Array.isArray(p.review_comments) && p.review_comments.length > 0) {
        setReviewsResult({
          reviews_state: p.reviews_state || 'available',
          rating: p.rating,
          review_count: p.review_count ?? p.review_comments.length,
          comments: p.review_comments,
          max_pages: 1,
        });
      } else if (p.rating != null || p.review_count != null) {
        setReviewsResult({
          reviews_state: p.reviews_state || 'available',
          rating: p.rating,
          review_count: p.review_count,
          comments: [],
          max_pages: 1,
        });
      }
    }
  }, [detail?.product]);

  useEffect(() => {
    let active = true;
    ApiClient.getProductDetails({ brand, url })
      .then(res => {
        if (active) setDetail(res);
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Erro ao carregar detalhes do produto.');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [brand, url]);

  useEffect(() => {
    const controller = new AbortController();
    ApiClient.getConditionalOffers({ brand, url, trigger: 'product_opened', force: false }, controller.signal)
      .then(result => {
        setOffersResult(prev => {
          if (result.offers && result.offers.length > 0) {
            return result;
          }
          if (prev && prev.offers && prev.offers.length > 0) {
            return {
              ...result,
              offers: prev.offers,
            };
          }
          return result;
        });
        setOffersAnnouncement('Verificação de ofertas concluída.');
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setOffersError(err instanceof Error ? err.message : 'Não foi possível verificar as ofertas agora.');
        setOffersAnnouncement('Falha ao verificar ofertas.');
      })
      .finally(() => {
        if (!controller.signal.aborted) setOffersLoading(false);
      });
    return () => controller.abort();
  }, [brand, url]);

  const revalidateOffers = async () => {
    setOffersLoading(true);
    setOffersError(null);
    setOffersAnnouncement('Revalidando ofertas...');
    try {
      const result = await ApiClient.getConditionalOffers({ brand, url, trigger: 'manual_revalidate', force: true });
      setOffersResult(prev => {
        if (result.offers && result.offers.length > 0) {
          return result;
        }
        if (prev && prev.offers && prev.offers.length > 0) {
          return {
            ...result,
            offers: prev.offers,
          };
        }
        return result;
      });
      setOffersAnnouncement('Ofertas revalidadas.');
    } catch (err: unknown) {
      setOffersError(err instanceof Error ? err.message : 'A revalidação falhou.');
      setOffersAnnouncement('A revalidação falhou; o resultado anterior foi mantido.');
    } finally {
      setOffersLoading(false);
    }
  };

  const handleFetchReviews = async () => {
    const p = detail?.product as Record<string, any> | undefined;
    const targetId = p?.review_product_id || p?.product_code || p?.product_id;
    if (!targetId) {
      if (p?.rating != null || p?.review_count != null) {
        setReviewsResult({
          reviews_state: 'available',
          rating: p.rating,
          review_count: p.review_count,
          comments: p.review_comments || [],
          max_pages: 1,
        });
        setReviewsError(null);
        return;
      }
      setReviewsResult({ reviews_state: 'unsupported', comments: [], max_pages: 0 });
      setReviewsError(null);
      return;
    }
    setReviewsLoading(true);
    setReviewsError(null);
    try {
      const result = await ApiClient.getProductReviews({ brand, product_id: targetId });
      setReviewsResult(result);
    } catch (err: unknown) {
      setReviewsError(err instanceof Error ? err.message : 'Não foi possível verificar as avaliações agora.');
    } finally {
      setReviewsLoading(false);
    }
  };

  const product = detail && (detail.state === 'ok' || detail.state === 'stale') ? detail.product : null;
  const unavailable = !loading && (!!error || (!!detail && detail.state !== 'ok' && detail.state !== 'stale'));

  const title = product?.raw_title || seed?.raw_title || seed?.product_name || 'Produto';
  const image = product?.image_url ?? seed?.image_url;
  const priceFull = toPositivePrice(product?.price_full ?? seed?.price_full);
  const priceDiscount = toPositivePrice(product?.price_discount ?? seed?.price_discount);
  const stock = product ? product.stock_availability : seed?.stock_availability ?? seed?.available;
  const breadcrumb = product?.breadcrumb && product.breadcrumb.length > 0 ? product.breadcrumb : undefined;
  const colors = product?.available_colors && product.available_colors.length > 0 ? product.available_colors : undefined;
  const sizes = product?.available_sizes && product.available_sizes.length > 0 ? product.available_sizes : undefined;
  const description = product?.raw_description ? stripHtmlToText(product.raw_description) : '';
  const composition = product?.composition;
  const specifications =
    product?.specifications && Object.keys(product.specifications).length > 0
      ? product.specifications
      : undefined;

  return (
    <Dialog
      title="Detalhes do Produto"
      onClose={onClose}
      size="md"
      className="product-detail-dialog"
    >
      {loading && !product ? (
        <div className="product-detail-summary">
          <Skeleton style={{ width: '120px', height: '120px', borderRadius: 'var(--radius-md)', flexShrink: 0 }} />
          <div className="product-detail-main" style={{ flex: 1 }}>
            <SkeletonText />
            <SkeletonText short />
            <SkeletonText short style={{ width: '30%', marginTop: '16px', height: '24px' }} />
          </div>
        </div>
      ) : (
        <div className="product-detail-summary">
          <div className="product-image product-detail-image">
            {image ? <img src={image} alt={title} /> : <Package size={40} />}
          </div>
          <div className="product-detail-main">
            <p className="product-name product-detail-name">{title}</p>
            {breadcrumb && (
              <p className="text-muted product-detail-breadcrumb">{breadcrumb.join(' › ')}</p>
            )}
            {priceFull != null && (
              <div className="product-detail-price">
                {priceDiscount != null && priceDiscount < priceFull ? (
                  <>
                    <span className="price-original">R$ {priceFull.toFixed(2)}</span>
                    <span className="price-current">R$ {priceDiscount.toFixed(2)}</span>
                  </>
                ) : (
                  <span className="price-current">R$ {priceFull.toFixed(2)}</span>
                )}
              </div>
            )}
            <div className="flex items-center gap-3 mt-1">
              {stock === true && <span className="text-success text-xs font-medium">Em estoque</span>}
              {stock === false && <span className="text-error text-xs font-medium">Esgotado</span>}
              {stock == null && <span className="text-muted text-xs">Estoque não verificado</span>}
              {url && (
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="product-detail-link--eyebrow product-detail-link--stock text-xs"
                >
                  <ExternalLink size={12} /> Ver no site
                </a>
              )}
              {showMonitorAction && url && brand && (
                <Button
                  variant="outline"
                  size="sm"
                  style={{ marginLeft: 'auto', gap: '4px', height: '26px', fontSize: '11px', padding: '0 8px' }}
                  title="Enviar este produto para o monitor de produto único"
                  disabled={addingToMonitor}
                  onClick={async () => {
                    setAddingToMonitor(true);
                    try {
                      const res = await ApiClient.addToMonitor(url, brand);
                      if (res.status === 'already_active') {
                        toast.info('Produto já está em monitoramento de produto único');
                      } else if (res.status === 'reactivated') {
                        toast.success('Monitor de produto único reativado com sucesso');
                      } else {
                        toast.success('Produto enviado para o monitor de produto único!');
                      }
                    } catch (err: any) {
                      toast.error(err.message || 'Erro ao enviar para monitor');
                    } finally {
                      setAddingToMonitor(false);
                    }
                  }}
                >
                  {addingToMonitor ? (
                    <RefreshCw size={12} className="animate-spin" />
                  ) : (
                    <Plus size={12} />
                  )}
                  Monitorar produto
                </Button>
              )}
            </div>
          </div>
        </div>
      )}

      <section className="product-detail-offers" aria-labelledby="conditional-offers-title">
        <div className="product-detail-offers__header">
          <div>
            <strong id="conditional-offers-title">Ofertas condicionais</strong>
            <p className="text-xs text-muted">Benefícios sujeitos à condição indicada; o preço público acima permanece a referência principal.</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void revalidateOffers()} disabled={offersLoading}>
            <RefreshCw size={14} className={offersLoading ? 'animate-spin' : ''} aria-hidden="true" />
            {offersLoading && offersResult ? 'Revalidando...' : 'Revalidar'}
          </Button>
        </div>
        <span className="sr-only" aria-live="polite">{offersAnnouncement}</span>
        {offersLoading && !offersResult && (
          <div className="product-detail-offers__list flex flex-col gap-2 py-4">
            <SkeletonRow />
            <SkeletonRow />
          </div>
        )}
        {offersResult?.offers.length ? (
          <ul className="product-detail-offers__list">
            {offersResult.offers
              .filter(offer => offer.state !== 'expired')
              .map(offer => (
                <ConditionalOfferRow
                  key={offer.offer_key || `${offer.condition_type}:${offer.condition_label}`}
                  offer={offer}
                />
              ))}
          </ul>
        ) : null}
        {offersResult && offerResultMessage(offersResult.status) && (
          <Status tone={offersResult.status === 'blocked' || offersResult.status === 'error' ? 'warning' : 'info'}>
            {offersResult.offers.length > 0 && (offersResult.status === 'blocked' || offersResult.status === 'error')
              ? `Última oferta mantida — ${offersResult.status === 'blocked' ? 'verificação bloqueada' : 'erro na verificação'}`
              : offerResultMessage(offersResult.status)}
          </Status>
        )}
        {offersError && (
          <Status tone="warning">
            {offersResult && offersResult.offers.length > 0
              ? 'A revalidação falhou; a oferta anterior foi mantida.'
              : 'Não foi possível verificar ofertas agora.'}
          </Status>
        )}
        {seed?.conditional_offer_history && seed.conditional_offer_history.length > 0 && (
          <div className="monitor-offer__history" style={{ marginTop: '1rem' }}>
            <strong>Histórico de ofertas condicionais</strong>
            <ol>
              {[...seed.conditional_offer_history].reverse().map((event, index) => {
                const offer = event.after || event.before;
                const before = formatOfferPrice(event.before);
                const after = formatOfferPrice(event.after);
                return (
                  <li key={`${event.created_at}:${event.offer_key}:${index}`}>
                    <div className="monitor-offer__event-heading">
                      <span>{monitorEventLabel[event.event_type]}</span>
                      <time dateTime={event.created_at}>
                        {new Date(event.created_at).toLocaleString('pt-BR')}
                      </time>
                    </div>
                    <p>
                      {offer?.condition_label || 'Oferta condicional'} - {offerStateMeta(offer?.state).label}
                    </p>
                    {before !== after && (
                      <p>{before || 'Sem valor'} para {after || 'Sem valor'}</p>
                    )}
                    {(event.observed_from || event.observed_until) && (
                      <p>
                        Período: {event.observed_from ? new Date(event.observed_from).toLocaleString('pt-BR') : 'início desconhecido'} até {event.observed_until ? new Date(event.observed_until).toLocaleString('pt-BR') : 'em andamento'}
                      </p>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        )}
      </section>

      {colors && (
        <div className="product-detail-section">
          <strong>Cores disponíveis:</strong>
          <div className="product-detail-tags">
            {colors.map(c => (
              <span key={c} className="badge">{c}</span>
            ))}
          </div>
        </div>
      )}

      {sizes && (
        <div className="product-detail-section">
          <strong>Tamanhos disponíveis:</strong>
          <div className="product-detail-tags">
            {sizes.map(s => (
              <span key={s} className="badge">{s}</span>
            ))}
          </div>
        </div>
      )}

      {description && (
        <div className="product-detail-section">
          <strong>Descrição:</strong>
          <p className="text-muted product-detail-copy product-detail-copy--pre">{description}</p>
        </div>
      )}

      {composition && (
        <div className="product-detail-section">
          <strong>Composição:</strong>
          <p className="text-muted product-detail-copy product-detail-copy--pre">{composition}</p>
        </div>
      )}

      {specifications && (
        <div className="product-detail-section">
          <strong>Especificações:</strong>
          <ul className="product-detail-specifications">
            {Object.entries(specifications).map(([k, v]) => (
              <li key={k}>
                <span className="font-medium">{k}:</span> {String(v)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {detail?.state === 'stale' && (
        <Status tone="warning">
          Exibindo a última leitura válida; o marketplace não pôde ser consultado agora.
          {detail?.next_retry_at ? ` Próxima tentativa: ${formatTimeGlobal(detail.next_retry_at)}.` : ''}
        </Status>
      )}

      {detail?.state === 'blocked' && (
        <Status tone="warning">
          Consulta bloqueada pela proteção anti-bot do marketplace; a última leitura válida foi mantida.
          {detail?.next_retry_at ? ` Próxima tentativa: ${formatTimeGlobal(detail.next_retry_at)}.` : ''}
        </Status>
      )}

      {unavailable && detail?.state !== 'blocked' && (
        <p className="text-muted product-detail-unavailable">
          Não foi possível carregar os detalhes deste produto agora.
        </p>
      )}

      <section className="product-detail-offers product-detail-reviews" aria-labelledby="product-reviews-title">
        <div className="product-detail-offers__header">
          <div>
            <strong id="product-reviews-title">
              <MessageSquare size={16} aria-hidden="true" /> Avaliações
            </strong>
            <p className="text-xs text-muted">Consulta sob demanda; nunca verificada automaticamente ao abrir o produto.</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void handleFetchReviews()} disabled={reviewsLoading}>
            <RefreshCw size={14} className={reviewsLoading ? 'animate-spin' : ''} aria-hidden="true" />
            {reviewsLoading ? 'Buscando...' : 'Buscar avaliações'}
          </Button>
        </div>

        {reviewsError && (
          <Status tone="error">
            <strong>Não foi possível verificar.</strong> Ocorreu um erro ao consultar as avaliações. Tente novamente.
          </Status>
        )}

        {!reviewsError && reviewsResult && (() => {
          const label = reviewsStateLabel(reviewsResult.reviews_state, reviewsResult.review_count);
          if (label === 'disponivel') {
            const displayedComments = showAllReviews
              ? reviewsResult.comments
              : reviewsResult.comments.slice(0, 3);
            return (
              <div className="product-detail-reviews-content">
                <div className="product-detail-reviews-summary">
                  <div className="product-detail-reviews-summary__left">
                    {reviewsResult.rating != null && (
                      <span className="product-detail-reviews-score">
                        <Star size={16} className="text-amber-500 fill-amber-500" aria-hidden="true" />
                        <strong>{reviewsResult.rating.toFixed(1)}</strong>
                        <span className="text-xs text-muted">/ 5</span>
                      </span>
                    )}
                    <span className="product-detail-offers__copy font-medium">
                      {reviewsResult.review_count ?? reviewsResult.comments.length} avaliação(ões)
                    </span>
                  </div>
                  {reviewsResult.source_provider && (
                    <Badge tone="info" className="text-xs">
                      via {reviewsResult.source_provider}
                    </Badge>
                  )}
                </div>

                {reviewsResult.comments.length > 0 && (
                  <>
                    <div className="product-detail-reviews-cards">
                      {displayedComments.map((comment, idx) => (
                        <article key={comment.review_id || idx} className="product-detail-review-card">
                          <header className="product-detail-review-card__header">
                            <div className="product-detail-review-card__author-info">
                              <strong className="product-detail-review-card__author">
                                {comment.author || 'Cliente verificado'}
                              </strong>
                              {comment.created_at && (
                                <time className="product-detail-review-card__date text-xs text-muted">
                                  {comment.created_at.includes('T') ? comment.created_at.split('T')[0] : comment.created_at}
                                </time>
                              )}
                            </div>
                            {comment.rating != null && (
                              <span className="product-detail-review-card__rating">
                                ★ {comment.rating}
                              </span>
                            )}
                          </header>
                          {comment.title && (
                            <h4 className="product-detail-review-card__title">
                              {comment.title}
                            </h4>
                          )}
                          {comment.text && (
                            <p className="product-detail-review-card__body">
                              {comment.text}
                            </p>
                          )}
                        </article>
                      ))}
                    </div>
                    {reviewsResult.comments.length > 3 && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setShowAllReviews(!showAllReviews)}
                        className="mt-2 text-xs"
                      >
                        {showAllReviews
                          ? 'Recolher avaliações'
                          : `Ver todas as ${reviewsResult.comments.length} avaliações`}
                      </Button>
                    )}
                  </>
                )}
              </div>
            );
          }
          if (label === 'vazio') {
            return (
              <EmptyState
                title="Sem avaliações no momento"
                description="A loja respondeu, mas ainda não há avaliações publicadas para este produto."
              />
            );
          }
          if (label === 'bloqueado') {
            return (
              <Status tone="warning">
                <strong>Consulta bloqueada.</strong> A loja está bloqueando a verificação de avaliações agora. Tente novamente mais tarde.
              </Status>
            );
          }
          if (label === 'nao suportado') {
            return (
              <Status tone="info">
                <strong>Avaliações não disponíveis.</strong> Esta loja não oferece avaliações para este produto.
              </Status>
            );
          }
          return (
            <Status tone="error">
              <strong>Não foi possível verificar.</strong> Ocorreu um erro ao consultar as avaliações. Tente novamente.
            </Status>
          );
        })()}
      </section>
    </Dialog>
  );
};

class ModalErrorBoundary extends Component<{ children: ReactNode; onClose: () => void }, { hasError: boolean }> {
  constructor(props: { children: ReactNode; onClose: () => void }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error, errorInfo: any) {
    console.error('Erro no modal de detalhes do produto:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <Dialog title="Detalhes do Produto" onClose={this.props.onClose} size="md" className="product-detail-dialog">
          <Status tone="error">
            <strong>Ocorreu um erro ao carregar os detalhes do produto.</strong>
            <p className="text-xs text-muted mt-1">Por favor, feche e tente novamente.</p>
          </Status>
        </Dialog>
      );
    }
    return this.props.children;
  }
}

export const ProductDetailModal = ({ open, brand, url, seed, showMonitorAction = true, onClose }: ProductDetailModalProps) => {
  if (!open || !url) return null;
  return (
    <ModalErrorBoundary onClose={onClose}>
      <ProductDetailModalContent
        key={`${brand}:${url}`}
        brand={brand}
        url={url}
        seed={seed}
        showMonitorAction={showMonitorAction}
        onClose={onClose}
      />
    </ModalErrorBoundary>
  );
};
