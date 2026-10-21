import { useEffect, useId, useRef, type ReactNode, type RefObject } from 'react';
import { X } from 'lucide-react';
import { IconButton } from './Button';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export type DialogProps = {
  title: ReactNode;
  children: ReactNode;
  onClose: () => void;
  description?: ReactNode;
  closeLabel?: string;
  closeOnOverlay?: boolean;
  closeOnEscape?: boolean;
  busy?: boolean;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  initialFocusRef?: RefObject<HTMLElement | null>;
  aboveHeader?: ReactNode;
};

export const Dialog = ({
  title,
  description,
  children,
  onClose,
  closeLabel = 'Fechar',
  closeOnOverlay = true,
  closeOnEscape = true,
  busy = false,
  size = 'md',
  className = '',
  initialFocusRef,
  aboveHeader,
}: DialogProps) => {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const aboveHeaderRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // Excludes anything inside aboveHeaderRef (e.g. an external link) so it never
    // hijacks initial dialog focus away from the dialog's actual title/controls/content.
    const candidates = panelRef.current
      ? Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      : [];
    const focusTarget = initialFocusRef?.current
      || candidates.find((el) => !aboveHeaderRef.current?.contains(el))
      || panelRef.current;
    focusTarget?.focus();

    return () => {
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [initialFocusRef]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && closeOnEscape && !busy) {
      event.preventDefault();
      onCloseRef.current();
      return;
    }
    if (event.key !== 'Tab' || !panelRef.current) return;
    const focusable = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
    if (focusable.length === 0) {
      event.preventDefault();
      panelRef.current.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className="dialog-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && closeOnOverlay && !busy) onCloseRef.current();
      }}
    >
      <div
        ref={panelRef}
        className={`dialog dialog--${size} ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        {aboveHeader && <div ref={aboveHeaderRef}>{aboveHeader}</div>}
        <header className="dialog__header">
          <h3 id={titleId} className="dialog__title">{title}</h3>
          <IconButton label={closeLabel} onClick={onClose} disabled={busy}>
            <X size={20} aria-hidden="true" />
          </IconButton>
        </header>
        {description && <p id={descriptionId} className="dialog__description">{description}</p>}
        <div className="dialog__body">{children}</div>
      </div>
    </div>
  );
};
