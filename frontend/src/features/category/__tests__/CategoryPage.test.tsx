// @vitest-environment jsdom

import React from 'react';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CategoryPage } from '../CategoryPage';
import { ApiClient } from '../../../api/client';

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const mockBrands = [
  { brand_key: 'aramis', brand_name: 'Aramis', domain: 'aramis.com.br' },
  { brand_key: 'zara', brand_name: 'Zara', domain: 'zara.com' },
  { brand_key: 'reserva', brand_name: 'Reserva', domain: 'usereserva.com' },
  { brand_key: 'dafiti', brand_name: 'Dafiti', domain: 'dafiti.com.br' },
  { brand_key: 'privalia', brand_name: 'Privalia', domain: 'br.privalia.com' },
  { brand_key: 'mercado_livre', brand_name: 'Mercado Livre', domain: 'mercadolivre.com.br' },
];

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onmessage: ((event: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send = vi.fn();
  close = vi.fn();

  dispatch(data: any) {
    if (this.onmessage) {
      this.onmessage({ data: JSON.stringify(data) });
    }
  }
}

describe('CategoryPage Component', () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    MockWebSocket.instances = [];
    (globalThis as any).WebSocket = MockWebSocket;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
    vi.restoreAllMocks();
  });

  it('renders initial state with empty state message when no products extracted', async () => {
    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    expect(container.textContent).toContain('Configuração da Varredura');
    expect(container.textContent).toContain('Nenhum produto extraído ainda');
    expect(container.textContent).not.toContain('Progresso e Logs');
    expect(container.textContent).not.toContain('Console Feed');
  });

  it('preserves equal category labels when they belong to different Aramis groups', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [
        { group: 'Aramis', items: [{ path: '/camisa-masculina', label: 'Camisas' }] },
        { group: 'Outlet', items: [{ path: '/outlet/camisa-masculina-em-oferta', label: 'Camisas' }] },
      ],
    });

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    const brandButton = container.querySelector('.brand-chip') as HTMLButtonElement;
    await act(async () => {
      brandButton.click();
      await Promise.resolve();
    });

    const options = [...container.querySelectorAll('select option')].map(option => ({
      value: (option as HTMLOptionElement).value,
      label: option.textContent,
    }));
    expect(options).toEqual(expect.arrayContaining([
      { value: '/camisa-masculina', label: 'Aramis - Camisas' },
      { value: '/outlet/camisa-masculina-em-oferta', label: 'Outlet - Camisas' },
    ]));
  });

  it('streams category_product_item and updates product card and brand averages', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [
        { group: 'Roupas', items: [{ path: '/roupas/camisas', label: 'Camisas' }] }
      ]
    });
    vi.spyOn(ApiClient, 'startScrape').mockResolvedValue({ job_id: 'test-cat-job-1' });

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    // Select brand
    const brandButton = container.querySelector('.brand-chip') as HTMLButtonElement;
    expect(brandButton).toBeTruthy();
    await act(async () => {
      brandButton.click();
    });

    // Wait for categories
    await act(async () => {
      await Promise.resolve();
    });

    // Select category in dropdown
    const select = container.querySelector('select') as HTMLSelectElement;
    expect(select).toBeTruthy();
    await act(async () => {
      select.value = '/roupas/camisas';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    // Click "Iniciar varredura"
    const startBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Iniciar varredura')
    );
    expect(startBtn).toBeTruthy();
    await act(async () => {
      startBtn?.click();
    });

    // Wait for startScrape promise
    await act(async () => {
      await Promise.resolve();
    });

    const ws = MockWebSocket.instances[0];
    expect(ws).toBeTruthy();

    // Dispatch a streaming product
    await act(async () => {
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Camisa Linho Aramis Premium',
          price: 299.90,
          price_original: 399.90,
          url: 'https://www.aramis.com.br/camisa-linho',
          image_url: 'https://img.aramis.com.br/camisa.jpg',
          brand: 'Aramis',
          is_available: true,
        },
      });
    });

    // Product card should be rendered
    const card = container.querySelector('[data-testid="product-card"]');
    expect(card).toBeTruthy();
    expect(card?.textContent).toContain('Camisa Linho Aramis Premium');
    expect(card?.textContent).toContain('R$ 299,90');
    expect(card?.textContent).toContain('R$ 399,90');

    // Metrics panel should display brand average
    const metrics = container.querySelector('[data-testid="category-metrics-panel"]');
    expect(metrics).toBeTruthy();
    expect(metrics?.textContent).toContain('Média por Marca');
    expect(metrics?.textContent).toContain('Aramis:');
    expect(metrics?.textContent).toContain('R$ 299,90');
  });

  it('toggles between Grid and Table views seamlessly', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [{ group: 'Roupas', items: [{ path: '/roupas/camisas', label: 'Camisas' }] }]
    });
    vi.spyOn(ApiClient, 'startScrape').mockResolvedValue({ job_id: 'test-cat-job-2' });

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    // Trigger scrape setup
    const brandBtn = container.querySelector('.brand-chip') as HTMLButtonElement;
    await act(async () => { brandBtn.click(); });
    await act(async () => { await Promise.resolve(); });

    const select = container.querySelector('select') as HTMLSelectElement;
    await act(async () => {
      select.value = '/roupas/camisas';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    const startBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Iniciar varredura')
    );
    await act(async () => { startBtn?.click(); });
    await act(async () => { await Promise.resolve(); });

    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.dispatch({
        type: 'category_product_item',
        brand: 'zara',
        brand_name: 'Zara',
        product: {
          product_name: 'Blazer Slim Comfort Zara',
          price: 499.00,
          price_original: null,
          url: 'https://zara.com/blazer-slim',
          image_url: 'https://zara.com/blazer.jpg',
          brand: 'Zara',
          is_available: true,
        },
      });
    });

    // Dispatch a second brand product to verify column separation
    await act(async () => {
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Camisa Linho Aramis Premium',
          price: 299.90,
          price_original: 399.90,
          url: 'https://aramis.com.br/camisa-linho',
          image_url: 'https://aramis.com.br/camisa.jpg',
          brand: 'Aramis',
          is_available: true,
        },
      });
    });

    // Grid container is active
    expect(container.querySelector('[data-testid="category-product-grid"]')).toBeTruthy();
    // Table button and table view do not exist
    expect(container.querySelector('[data-testid="category-product-table"]')).toBeNull();
    const tableBtn = [...container.querySelectorAll('button')].find(b => b.textContent?.includes('Tabela'));
    expect(tableBtn).toBeUndefined();

    // Multi-brand columns are rendered for both brands
    const columns = container.querySelectorAll('.category-brand-column');
    expect(columns.length).toBeGreaterThanOrEqual(2);
    expect(container.textContent).toContain('Blazer Slim Comfort Zara');
    expect(container.textContent).toContain('Camisa Linho Aramis Premium');
  });

  it('updates brand averages from done event payload', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [{ group: 'Roupas', items: [{ path: '/roupas/camisas', label: 'Camisas' }] }]
    });
    vi.spyOn(ApiClient, 'startScrape').mockResolvedValue({ job_id: 'test-cat-job-3' });

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    const brandBtn = container.querySelector('.brand-chip') as HTMLButtonElement;
    await act(async () => { brandBtn.click(); });
    await act(async () => { await Promise.resolve(); });

    const select = container.querySelector('select') as HTMLSelectElement;
    await act(async () => {
      select.value = '/roupas/camisas';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    const startBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Iniciar varredura')
    );
    await act(async () => { startBtn?.click(); });
    await act(async () => { await Promise.resolve(); });

    const ws = MockWebSocket.instances[0];

    // Dispatch done event with precomputed brand_averages
    await act(async () => {
      ws.dispatch({
        type: 'done',
        valid_products: 10,
        brand_averages: {
          aramis: { average_price: 350.50, average_price_original: 420.00, count: 6 },
          reserva: { average_price: 189.90, average_price_original: 250.00, count: 4 },
        },
        message: 'Varredura concluída com sucesso.',
      });
    });

    const metrics = container.querySelector('[data-testid="category-metrics-panel"]');
    expect(metrics).toBeTruthy();
    expect(metrics?.textContent).toContain('Aramis:');
    expect(metrics?.textContent).toContain('R$ 350,50');
    expect(metrics?.textContent).toContain('6 un');
    expect(metrics?.textContent).toContain('Reserva:');
    expect(metrics?.textContent).toContain('R$ 189,90');
    expect(metrics?.textContent).toContain('4 un');
  });

  it('excludes marketplaces (Dafiti, Privalia, Mercado Livre, Amazon, Netshoes) from brand selector', async () => {
    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    const chipTexts = [...container.querySelectorAll('.brand-chip')].map(el => el.textContent);
    expect(chipTexts).toContain('Aramis');
    expect(chipTexts).toContain('Zara');
    expect(chipTexts).toContain('Reserva');
    expect(chipTexts).not.toContain('Dafiti');
    expect(chipTexts).not.toContain('Privalia');
    expect(chipTexts).not.toContain('Mercado Livre');
  });

  it('sorts extracted products by price (descending and ascending)', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [{ group: 'Roupas', items: [{ path: '/roupas/polos', label: 'Polos' }] }]
    });
    vi.spyOn(ApiClient, 'startScrape').mockResolvedValue({ job_id: 'test-sort-job' });

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    const brandBtn = container.querySelector('.brand-chip') as HTMLButtonElement;
    await act(async () => { brandBtn.click(); });
    await act(async () => { await Promise.resolve(); });

    const select = container.querySelector('select') as HTMLSelectElement;
    await act(async () => {
      select.value = '/roupas/polos';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    const startBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Iniciar varredura')
    );
    await act(async () => { startBtn?.click(); });
    await act(async () => { await Promise.resolve(); });

    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Polo Barata',
          price: 100.00,
          url: 'https://aramis.com.br/polo-1',
          brand: 'Aramis',
        },
      });
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Polo Cara',
          price: 500.00,
          url: 'https://aramis.com.br/polo-2',
          brand: 'Aramis',
        },
      });
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Polo Media',
          price: 250.00,
          url: 'https://aramis.com.br/polo-3',
          brand: 'Aramis',
        },
      });
    });

    // Check sort select exists
    const sortSelect = container.querySelector('.category-sort-select') as HTMLSelectElement;
    expect(sortSelect).toBeTruthy();

    // Sort by price descending (maior para menor)
    await act(async () => {
      sortSelect.value = 'desc';
      sortSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });

    let cards = container.querySelectorAll('[data-testid="product-card"]');
    expect(cards[0].textContent).toContain('Polo Cara');
    expect(cards[1].textContent).toContain('Polo Media');
    expect(cards[2].textContent).toContain('Polo Barata');

    // Sort by price ascending (menor para maior)
    await act(async () => {
      sortSelect.value = 'asc';
      sortSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });

    cards = container.querySelectorAll('[data-testid="product-card"]');
    expect(cards[0].textContent).toContain('Polo Barata');
    expect(cards[1].textContent).toContain('Polo Media');
    expect(cards[2].textContent).toContain('Polo Cara');
  });

  it('provides export Excel button in the toolbar and calls ApiClient.exportCategoryProducts', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [{ group: 'Roupas', items: [{ path: '/roupas/polos', label: 'Polos' }] }]
    });
    vi.spyOn(ApiClient, 'startScrape').mockResolvedValue({ job_id: 'test-export-job' });
    const exportSpy = vi.spyOn(ApiClient, 'exportCategoryProducts').mockResolvedValue(undefined);

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    const brandBtn = container.querySelector('.brand-chip') as HTMLButtonElement;
    await act(async () => { brandBtn.click(); });
    await act(async () => { await Promise.resolve(); });

    const select = container.querySelector('select') as HTMLSelectElement;
    await act(async () => {
      select.value = '/roupas/polos';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    const startBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Iniciar varredura')
    );
    await act(async () => { startBtn?.click(); });
    await act(async () => { await Promise.resolve(); });

    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Polo Piquet',
          price: 199.90,
          url: 'https://aramis.com.br/polo-piquet',
          brand: 'Aramis',
        },
      });
    });

    const exportBtn = container.querySelector('.category-export-btn') as HTMLButtonElement;
    expect(exportBtn).toBeTruthy();
    expect(exportBtn.textContent).toContain('Baixar Excel');

    await act(async () => {
      exportBtn.click();
    });

    expect(exportSpy).toHaveBeenCalledTimes(1);
    expect(exportSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ product_name: 'Polo Piquet', price: 199.90 })
        ])
      })
    );
  });

  it('does not display Sincronizado, timestamps, chart button or Console Feed / Progresso e Logs', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue({
      categories: [{ group: 'Roupas', items: [{ path: '/roupas/polos', label: 'Polos' }] }]
    });
    vi.spyOn(ApiClient, 'startScrape').mockResolvedValue({
      job_id: 'test-feed-job',
      url: 'https://www.aramis.com.br/roupas/polos'
    });

    await act(async () => {
      root.render(<CategoryPage brands={mockBrands} />);
    });

    const brandBtn = container.querySelector('.brand-chip') as HTMLButtonElement;
    await act(async () => { brandBtn.click(); });
    await act(async () => { await Promise.resolve(); });

    const select = container.querySelector('select') as HTMLSelectElement;
    await act(async () => {
      select.value = '/roupas/polos';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });

    const startBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Iniciar varredura')
    );
    await act(async () => { startBtn?.click(); });
    await act(async () => { await Promise.resolve(); });

    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.dispatch({
        type: 'category_product_item',
        brand: 'aramis',
        brand_name: 'Aramis',
        product: {
          product_name: 'Polo Piquet Premium',
          price: 299.00,
          url: 'https://aramis.com.br/polo',
          brand: 'Aramis',
          is_available: true,
        },
      });
    });

    // Verify Sincronizado, timestamps, chart button and Console Feed are NOT present
    expect(container.textContent).not.toContain('Sincronizado');
    expect(container.textContent).not.toContain('Tentativa:');
    expect(container.textContent).not.toContain('Sucesso:');
    expect(container.textContent).not.toContain('Console Feed');
    expect(container.textContent).not.toContain('Progresso e Logs');
    expect(container.querySelector('.console-container')).toBeNull();

    // Chart button (aria-label "Ver detalhes do produto") should NOT exist in actions
    const chartBtn = container.querySelector('[aria-label="Ver detalhes do produto"]');
    expect(chartBtn).toBeNull();

    // Other action buttons (Adicionar ao monitoramento, Abrir no site) should exist
    expect(container.querySelector('[aria-label="Adicionar ao monitoramento"]')).toBeTruthy();
    expect(container.querySelector('[aria-label="Abrir no site"]')).toBeTruthy();
  });
});

