import { useEffect } from 'react';
import { Status } from '../../components/ui/Feedback';
import { CheckCircle2, XCircle, RefreshCw } from 'lucide-react';

export const StatusBanner = ({ type, message, onClear }: { type: 'success' | 'error' | 'info', message: string, onClear?: () => void }) => {
  useEffect(() => {
    if (message && onClear) {
      const timer = setTimeout(onClear, 5000);
      return () => clearTimeout(timer);
    }
  }, [message, onClear]);

  if (!message) return null;

  return (
    <Status
      tone={type}
      icon={type === 'success'
        ? <CheckCircle2 size={18} aria-hidden="true" />
        : type === 'error'
          ? <XCircle size={18} aria-hidden="true" />
          : <RefreshCw className="animate-spin" size={18} aria-hidden="true" />}
    >
      {message}
    </Status>
  );
};
