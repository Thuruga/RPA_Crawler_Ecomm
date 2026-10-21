import { Terminal } from 'lucide-react';
import type { JobProgress } from '../../api/client';

export type { JobProgress };

export const JobProgressTerminal = ({ status, progress, error, retry }: { status: string, progress?: JobProgress | null | undefined, error: string | null, retry?: () => void }) => {
  if (status === 'idle' || status === 'ready' || status === 'completed') return null;

  if (status === 'failed') {
    return (
      <div className="job-terminal error-state" style={{ padding: '1rem', background: '#fee', color: '#c00', border: '1px solid #fcc', borderRadius: '4px', marginBottom: '1rem' }}>
        <div>{error || 'Falha ao acessar o site da marca.'}</div>
        {retry && <button type="button" className="btn btn-sm" onClick={retry} style={{ marginTop: '0.5rem' }}>Tentar novamente</button>}
      </div>
    );
  }

  // Se estiver buscando e o único dado for o identificador interno 'resolving_reference' ou 0 sem total, oculta
  const isInternalStage = progress?.stage === 'resolving_reference';
  const hasValidUnits = progress?.completed_units != null && (progress.completed_units > 0 || progress?.total_units != null);
  const displayStage = isInternalStage ? null : progress?.stage;

  if (!displayStage && !hasValidUnits && status !== 'interrupted') {
    return null;
  }

  return (
    <div className="job-terminal" style={{ padding: '1rem', background: 'var(--surface-sunken, #f8f9fa)', borderRadius: '4px', marginBottom: '1rem', display: 'flex', gap: '0.75rem', alignItems: 'flex-start' }}>
      <Terminal size={18} className="text-muted" style={{ marginTop: '2px' }} />
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 500, marginBottom: '0.25rem', color: 'var(--text-primary)' }}>
          {status === 'interrupted' ? 'Busca interrompida. Retomando...' : 'Buscando...'}
        </div>
        {displayStage && (
          <div style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>{displayStage}</div>
        )}
        {hasValidUnits && (
          <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>
            {progress?.completed_units} {progress?.total_units != null ? `de ${progress.total_units}` : ''}
            {progress?.percent != null ? ` (${progress.percent}%)` : ''}
          </div>
        )}
      </div>
    </div>
  );
};
