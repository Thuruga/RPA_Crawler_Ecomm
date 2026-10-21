import type { HTMLAttributes, ReactNode } from 'react';

export type TableRegionProps = Omit<HTMLAttributes<HTMLDivElement>, 'children'> & {
  label: string;
  children: ReactNode;
};

export const TableRegion = ({ label, children, className = '', ...props }: TableRegionProps) => (
  <div
    className={`table-region ${className}`.trim()}
    role="region"
    aria-label={label}
    tabIndex={0}
    {...props}
  >
    {children}
  </div>
);
