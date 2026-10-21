import type { ReactNode } from 'react';
import { Panel } from '../../components/ui/Panel';

export type GlassCardProps = {
  children?: ReactNode;
  title?: ReactNode;
  action?: ReactNode;
  className?: string;
  subtitle?: ReactNode;
};

export const GlassCard = ({ children, title, action, className = "", subtitle }: GlassCardProps) => (
  <Panel title={title} subtitle={subtitle} action={action} className={`glass-card ${className}`}>
    {children}
  </Panel>
);

