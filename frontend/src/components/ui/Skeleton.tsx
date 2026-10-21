import React from 'react';

export const Skeleton = ({ className = '', ...props }: React.HTMLAttributes<HTMLDivElement>) => {
  return <div className={`skeleton ${className}`} {...props} />;
};

export const SkeletonText = ({ className = '', short = false, ...props }: React.HTMLAttributes<HTMLDivElement> & { short?: boolean }) => {
  return <div className={`skeleton skeleton-text ${short ? 'short' : ''} ${className}`} {...props} />;
};

export const SkeletonAvatar = ({ className = '', ...props }: React.HTMLAttributes<HTMLDivElement>) => {
  return <div className={`skeleton skeleton-avatar ${className}`} {...props} />;
};

export const SkeletonCard = ({ className = '', children, ...props }: React.HTMLAttributes<HTMLDivElement>) => {
  return (
    <div className={`skeleton-card ${className}`} {...props}>
      {children || (
        <>
          <Skeleton style={{ height: '150px', width: '100%', borderRadius: 'var(--radius-sm)' }} />
          <SkeletonText />
          <SkeletonText short />
        </>
      )}
    </div>
  );
};

export const SkeletonRow = ({ className = '', children, ...props }: React.HTMLAttributes<HTMLDivElement>) => {
  return (
    <div className={`skeleton-row ${className}`} {...props}>
      {children || (
        <>
          <SkeletonAvatar />
          <div style={{ flex: 1 }}>
            <SkeletonText />
            <SkeletonText short />
          </div>
        </>
      )}
    </div>
  );
};

export const SkeletonChart = ({ className = '', ...props }: React.HTMLAttributes<HTMLDivElement>) => {
  return (
    <div className={`skeleton-card ${className}`} style={{ height: '300px', display: 'flex', alignItems: 'flex-end', gap: '8px', padding: '16px' }} {...props}>
      <Skeleton style={{ flex: 1, height: '40%' }} />
      <Skeleton style={{ flex: 1, height: '70%' }} />
      <Skeleton style={{ flex: 1, height: '50%' }} />
      <Skeleton style={{ flex: 1, height: '90%' }} />
      <Skeleton style={{ flex: 1, height: '60%' }} />
      <Skeleton style={{ flex: 1, height: '30%' }} />
      <Skeleton style={{ flex: 1, height: '80%' }} />
    </div>
  );
};
