/**
 * Utilitários de formatação de data/hora e texto.
 * Extraído do App.tsx monolítico durante reorganização DDD.
 */

export const formatTimeGlobal = (isoString?: string) => {
  if (!isoString) return '--:--';
  const d = new Date(isoString);
  const now = new Date();

  const isToday =
    d.getDate() === now.getDate() &&
    d.getMonth() === now.getMonth() &&
    d.getFullYear() === now.getFullYear();

  const timeStr = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  if (isToday) {
    return `Hoje, ${timeStr}`;
  }

  const dateStr = d.toLocaleDateString([], { day: '2-digit', month: '2-digit' });
  return `${dateStr}, ${timeStr}`;
};

export const formatHistoryDate = (iso: string) => {
  try {
    return new Date(iso).toLocaleString('pt-BR', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
};

export const formatCountLabel = (count: number, singular: string, plural: string) =>
  `${count} ${count === 1 ? singular : plural}`;

// Descrição pode vir como HTML cru (ex: body_html da Shopify) — nunca injetamos
// HTML sem sanitização (T-iiz-03); renderizamos como texto puro (strip básico de tags).
export const stripHtmlToText = (html?: string | null): string => {
  if (!html) return '';
  return html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
};
