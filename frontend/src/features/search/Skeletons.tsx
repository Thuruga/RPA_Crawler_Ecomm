
export const ResultsSkeleton = () => (
  <div className="results-container">
    {[1, 2, 3].map(i => (
      <div key={i} className="brand-column skeleton-card" style={{ padding: '0', border: 'none', background: 'transparent' }}>
        <div className="brand-column-header skeleton" style={{ height: '40px', marginBottom: '1rem', borderRadius: 'var(--radius-md)' }}></div>
        <div className="product-card skeleton-card">
          <div className="skeleton skeleton-text" style={{ height: '160px', borderRadius: 'var(--radius-md)' }}></div>
          <div className="skeleton skeleton-text short"></div>
          <div className="skeleton skeleton-text"></div>
        </div>
        <div className="product-card skeleton-card">
          <div className="skeleton skeleton-text" style={{ height: '160px', borderRadius: 'var(--radius-md)' }}></div>
          <div className="skeleton skeleton-text short"></div>
          <div className="skeleton skeleton-text"></div>
        </div>
      </div>
    ))}
  </div>
);

export const CrossResultsSkeleton = () => (
  <div className="sku-results-stack">
    <div className="sku-market-grid">
      {[1, 2, 3].map(i => (
        <div key={i} className="glass-card skeleton-card" style={{ gap: '1.5rem' }}>
          <div className="skeleton skeleton-text short" style={{ height: '24px' }}></div>
          <div className="sku-offer-card skeleton-card" style={{ border: 'none' }}>
            <div className="skeleton skeleton-text" style={{ height: '80px', borderRadius: 'var(--radius-md)' }}></div>
          </div>
        </div>
      ))}
    </div>
  </div>
);
