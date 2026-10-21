// @vitest-environment jsdom

import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { MonitoredCategoriesPage } from '../MonitoredCategoriesPage';
import { ApiClient } from '../../../api/client';

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

const mockBrands = [
  { brand_key: 'aramis', brand_name: 'Aramis', domain: 'aramis.com.br' },
  { brand_key: 'zara', brand_name: 'Zara', domain: 'zara.com' },
  { brand_key: 'dafiti', brand_name: 'Dafiti', domain: 'dafiti.com.br' },
  { brand_key: 'privalia', brand_name: 'Privalia', domain: 'br.privalia.com' },
  { brand_key: 'mercado_livre', brand_name: 'Mercado Livre', domain: 'mercadolivre.com.br' },
  { brand_key: 'amazon', brand_name: 'Amazon', domain: 'amazon.com.br' },
  { brand_key: 'netshoes', brand_name: 'Netshoes', domain: 'netshoes.com.br' },
];

describe('MonitoredCategoriesPage Component', () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    vi.spyOn(ApiClient, 'getMonitoredCategories').mockResolvedValue([]);
    vi.spyOn(ApiClient, 'request').mockResolvedValue([]);
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

  it('excludes marketplaces from brand selection in the add category monitor modal', async () => {
    await act(async () => {
      root.render(
        <MemoryRouter>
          <MonitoredCategoriesPage brands={mockBrands} />
        </MemoryRouter>
      );
    });
    await act(async () => { await Promise.resolve(); });

    // Open add modal
    const newBtn = [...container.querySelectorAll('button')].find(b =>
      b.textContent?.includes('Novo monitor')
    );
    expect(newBtn).toBeTruthy();

    await act(async () => {
      newBtn?.click();
    });
    await act(async () => { await Promise.resolve(); });

    const select = container.querySelector('select') as HTMLSelectElement;
    expect(select).toBeTruthy();

    const options = [...select.querySelectorAll('option')].map(o => o.textContent);
    expect(options).toContain('Aramis');
    expect(options).toContain('Zara');
    expect(options).not.toContain('Dafiti');
    expect(options).not.toContain('Privalia');
    expect(options).not.toContain('Mercado Livre');
    expect(options).not.toContain('Amazon');
    expect(options).not.toContain('Netshoes');
  });
});
