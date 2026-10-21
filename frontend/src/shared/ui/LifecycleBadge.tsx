import { Badge } from '../../components/ui/Feedback';
import type { UnifiedLifecycleState } from '../../api/client';

export const LifecycleBadge = ({ state }: { state: UnifiedLifecycleState }) => {
  if (state === 'success') return <Badge tone="success">Sincronizado</Badge>;
  if (state === 'stale') return <Badge tone="warning">Desatualizado</Badge>;
  if (state === 'error_blocked') return <Badge tone="error">Erro/Bloqueado</Badge>;
  if (state === 'running') return <Badge tone="info">Rodando</Badge>;
  if (state === 'pending') return <Badge tone="info">Pendente</Badge>;
  return <Badge tone="info">{state}</Badge>;
};
