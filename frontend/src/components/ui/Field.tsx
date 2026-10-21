import { useId, type ReactNode } from 'react';

export type FieldProps = {
  label: string;
  children: (accessibility: {
    id: string;
    'aria-invalid': true | undefined;
    'aria-describedby': string | undefined;
  }) => ReactNode;
  id?: string;
  help?: string;
  error?: string;
  required?: boolean;
  className?: string;
};

export const Field = ({ label, children, id, help, error, required, className = '' }: FieldProps) => {
  const generatedId = useId();
  const controlId = id || generatedId;
  const helpId = help ? `${controlId}-help` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy = [helpId, errorId].filter(Boolean).join(' ') || undefined;

  return (
    <div className={`field ${error ? 'field--error' : ''} ${className}`.trim()}>
      <label className="field__label" htmlFor={controlId}>
        {label}{required && <span aria-hidden="true"> *</span>}
      </label>
      {children({ id: controlId, 'aria-invalid': error ? true : undefined, 'aria-describedby': describedBy })}
      {help && <span id={helpId} className="field__help">{help}</span>}
      {error && <span id={errorId} className="field__error" role="alert">{error}</span>}
    </div>
  );
};
