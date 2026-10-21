import { forwardRef, type HTMLAttributes, type ReactNode } from 'react';

export type PanelProps = Omit<HTMLAttributes<HTMLElement>, 'title'> & {
  title?: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
};

export const Panel = forwardRef<HTMLElement, PanelProps>(({
  title,
  subtitle,
  action,
  className = '',
  children,
  ...props
}, ref) => (
  <section ref={ref} className={`ui-panel ${className}`.trim()} {...props}>
    {(title || subtitle || action) && (
      <header className="ui-panel__header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          {title && <h3 className="ui-panel__title">{title}</h3>}
          {subtitle && <p className="ui-panel__subtitle">{subtitle}</p>}
        </div>
        {action && <div>{action}</div>}
      </header>
    )}
    {children}
  </section>
));

Panel.displayName = 'Panel';
