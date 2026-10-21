export const reviewsStateLabel = (state?: string | null, reviewCount?: number | null) => {
  if (state === 'available' && (reviewCount ?? 0) === 0) return 'vazio';
  const labels: Record<string, string> = {
    available: 'disponivel',
    unsupported: 'nao suportado',
    blocked: 'bloqueado',
    error: 'erro',
    temporary_failure: 'erro',
  };
  return state ? (labels[state] || state) : '';
};

export type ReviewCommentLike = {
  review_id?: string | null;
  rating?: number | null;
  title?: string | null;
  text?: string | null;
  author?: string | null;
  created_at?: string | null;
  source_provider?: string | null;
  [key: string]: unknown;
};

export type ProductWithReviewsLike = {
  rating?: number | null;
  review_count?: number | null;
  reviews_state?: string | null;
  comments?: ReviewCommentLike[] | null;
  review_comments?: ReviewCommentLike[] | null;
  [key: string]: unknown;
};

export const productReviewComments = (product: ProductWithReviewsLike): ReviewCommentLike[] => {
  if (Array.isArray(product.comments)) return product.comments;
  if (Array.isArray(product.review_comments)) return product.review_comments;
  return [];
};

export const reviewCommentDisplayText = (comment: ReviewCommentLike): string => {
  if (comment.title && comment.text) {
    return `${comment.title} — ${comment.text}`;
  }
  return (
    comment.text ||
    comment.title ||
    [comment.author, comment.created_at].filter(Boolean).join(' - ') ||
    'Avaliação sem texto'
  );
};

