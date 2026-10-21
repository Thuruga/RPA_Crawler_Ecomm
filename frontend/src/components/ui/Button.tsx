import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';

type ButtonVariant = 'primary' | 'secondary' | 'outline' | 'destructive';
type ButtonSize = 'sm' | 'md';

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
  fullWidth?: boolean;
  loading?: boolean;
  loadingLabel?: string;
  icon?: ReactNode;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(({
  variant = 'primary',
  size = 'md',
  fullWidth = false,
  loading = false,
  loadingLabel = 'Carregando',
  icon,
  className = '',
  disabled,
  children,
  type = 'button',
  ...props
}, ref) => (
  <button
    ref={ref}
    type={type}
    className={`button button--${variant} button--${size} ${fullWidth ? 'button--full-width' : ''} ${className}`.trim()}
    disabled={disabled || loading}
    aria-busy={loading || undefined}
    {...props}
  >
    {loading ? <span className="ui-spinner" aria-hidden="true" /> : icon}
    <span>{loading ? loadingLabel : children}</span>
  </button>
));

Button.displayName = 'Button';

export type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  variant?: Extract<ButtonVariant, 'secondary' | 'outline' | 'destructive'>;
};

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(({
  label,
  variant = 'outline',
  className = '',
  children,
  type = 'button',
  ...props
}, ref) => (
  <button
    ref={ref}
    type={type}
    className={`icon-button icon-button--${variant} ${className}`.trim()}
    aria-label={label}
    {...props}
  >
    {children}
  </button>
));

IconButton.displayName = 'IconButton';
