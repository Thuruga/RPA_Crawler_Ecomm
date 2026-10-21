import { useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button } from '../../components/ui/Button';
import { Dialog } from '../../components/ui/Dialog';

export type ConfirmState = { message: string; action: () => void } | null;

export const ConfirmDialog = ({ state, onClose, cancelLabel, title = 'Confirmar exclusão', confirmLabel = 'Excluir' }: {
  state: ConfirmState;
  onClose: () => void;
  cancelLabel: string;
  title?: string;
  confirmLabel?: string;
}) => {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmedStateRef = useRef<ConfirmState>(null);
  if (!state) return null;
  const handleConfirm = () => {
    if (confirmedStateRef.current === state) return;
    confirmedStateRef.current = state;
    const action = state.action;
    onClose();
    action();
  };
  return (
    <Dialog
      title={<><AlertTriangle size={20} className="text-error" aria-hidden="true" /> {title}</>}
      description={state.message}
      onClose={onClose}
      initialFocusRef={cancelRef}
      size="sm"
    >
      <div className="dialog__actions">
        <Button ref={cancelRef} variant="outline" onClick={onClose}>{cancelLabel}</Button>
        <Button variant="destructive" onClick={handleConfirm}>{confirmLabel}</Button>
      </div>
    </Dialog>
  );
};
