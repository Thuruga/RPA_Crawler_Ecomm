import type { ReactNode } from 'react';

export type FeedbackTone = 'success' | 'error' | 'warning' | 'info';

export const Badge = ({ tone = 'info', children, className = '' }: {
  tone?: FeedbackTone;
  children: ReactNode;
  className?: string;
}) => <span className={`ui-badge ui-feedback--${tone} ${className}`.trim()}>{children}</span>;

export const Status = ({ tone, children, icon }: {
  tone: FeedbackTone;
  children: ReactNode;
  icon?: ReactNode;
}) => (
  <div className={`ui-status ui-feedback--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
    {icon}<span>{children}</span>
  </div>
);

export const EmptyState = ({ title, description, icon }: {
  title: string;
  description?: string;
  icon?: ReactNode;
}) => (
  <div className="ui-empty-state">
    {icon}
    <strong>{title}</strong>
    {description && <p>{description}</p>}
  </div>
);

export const LoadingState = ({ label = 'Carregando' }: { label?: string }) => (
  <div className="ui-loading-state" role="status">
    <span className="ui-spinner" aria-hidden="true" />
    <span>{label}</span>
  </div>
);

export const ErrorState = ({ title = 'Não foi possível concluir', description }: {
  title?: string;
  description?: string;
}) => (
  <div className="ui-empty-state ui-feedback--error" role="alert">
    <strong>{title}</strong>
    {description && <p>{description}</p>}
  </div>
);

export const Progress = ({ value, label }: { value: number; label: string }) => {
  const normalized = Math.min(100, Math.max(0, value));
  return (
    <div className="ui-progress">
      <div
        className="ui-progress__track"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={normalized}
      >
        <span className="ui-progress__fill" style={{ width: `${normalized}%` }} />
      </div>
      <span className="ui-progress__label">{normalized}%</span>
    </div>
  );
};
