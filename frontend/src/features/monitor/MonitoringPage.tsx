import React, { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Package, Layers } from 'lucide-react';
import { MonitorPage } from './MonitorPage';
import { MonitoredCategoriesPage } from './MonitoredCategoriesPage';
import type { BrandItem } from '../../shared/lib';

type MonitorView = 'product' | 'category';

export const MonitoringPage = ({ brands }: { brands: BrandItem[] }) => {
  const [view, setView] = useState<MonitorView>('product');
  const [searchParams] = useSearchParams();
  const categoryNotificationTarget = searchParams.get('categoryMonitorId') && searchParams.get('categoryProductUrl');
  const productTabRef = useRef<HTMLButtonElement>(null);
  const categoryTabRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (categoryNotificationTarget) setView('category');
  }, [categoryNotificationTarget]);

  const handleMonitorTabKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, target: MonitorView) => {
    const nextView = event.key === 'ArrowRight'
      ? (target === 'product' ? 'category' : 'product')
      : event.key === 'ArrowLeft'
        ? (target === 'product' ? 'category' : 'product')
        : null;
    if (nextView) {
      event.preventDefault();
      setView(nextView);
      (nextView === 'product' ? productTabRef : categoryTabRef).current?.focus();
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      setView(target);
    }
  };

  return (
    <div className="page-content monitor-page" aria-labelledby="monitoring-workspace-title">
      <h2 id="monitoring-workspace-title" className="sr-only">Monitoramento</h2>
      <div className="view-toggle-row">
        <div className="view-toggle" role="tablist" aria-label="Tipo de monitoramento">
          <button
            ref={productTabRef}
            id="monitor-product-tab"
            type="button"
            role="tab"
            aria-selected={view === 'product'}
            aria-controls="monitor-product-panel"
            tabIndex={view === 'product' ? 0 : -1}
            className={`view-toggle-btn ${view === 'product' ? 'active' : ''}`}
            onClick={() => setView('product')}
            onKeyDown={(event) => handleMonitorTabKeyDown(event, 'product')}
          >
            <Package size={16} /> <span>Produto Único</span>
          </button>
          <button
            ref={categoryTabRef}
            id="monitor-category-tab"
            type="button"
            role="tab"
            aria-selected={view === 'category'}
            aria-controls="monitor-category-panel"
            tabIndex={view === 'category' ? 0 : -1}
            className={`view-toggle-btn ${view === 'category' ? 'active' : ''}`}
            onClick={() => setView('category')}
            onKeyDown={(event) => handleMonitorTabKeyDown(event, 'category')}
          >
            <Layers size={16} /> <span>Categorias</span>
          </button>
        </div>
      </div>

      <div
        id={view === 'product' ? 'monitor-product-panel' : 'monitor-category-panel'}
        role="tabpanel"
        aria-labelledby={view === 'product' ? 'monitor-product-tab' : 'monitor-category-tab'}
        tabIndex={0}
      >
        {view === 'product' ? <MonitorPage brands={brands} /> : <MonitoredCategoriesPage brands={brands} />}
      </div>
    </div>
  );
};
