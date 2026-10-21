import React, { useState } from 'react';
import { toast } from 'sonner';
import { Plus, Globe, Power, Trash2 } from 'lucide-react';
import { ApiClient } from '../../api/client';
import { Panel } from '../../components/ui/Panel';
import { Button } from '../../components/ui/Button';
import { Dialog } from '../../components/ui/Dialog';
import { EmptyState, Status } from '../../components/ui/Feedback';
import { ConfirmDialog, type ConfirmState } from '../../shared/ui/ConfirmDialog';
import type { BrandItem } from '../../shared/lib';

export const SettingsPage = ({ brands, onRefresh }: { brands: BrandItem[], onRefresh: () => void }) => {
  const [confirmState, setConfirmState] = useState<ConfirmState>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [savingBrand, setSavingBrand] = useState(false);
  const [newBrand, setNewBrand] = useState({ brand_key: '', brand_name: '', domain: '' });

  const handleAddBrand = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!newBrand.brand_key.trim() || !newBrand.brand_name.trim() || !newBrand.domain.trim()) return;
    setSavingBrand(true);
    try {
      await ApiClient.saveBrand({
        brand_key: newBrand.brand_key.trim(),
        brand_name: newBrand.brand_name.trim(),
        domain: newBrand.domain.trim(),
        engine: 'auto',
      });
      setNewBrand({ brand_key: '', brand_name: '', domain: '' });
      setAddOpen(false);
      onRefresh();
      toast.success('Marca adicionada.');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error('Erro ao adicionar marca: ' + message);
    } finally {
      setSavingBrand(false);
    }
  };

  const handleDeleteBrand = (brand: BrandItem) => {
    setConfirmState({
      message: `Tem certeza que deseja excluir a marca ${brand.brand_name || brand.brand_key}?`,
      action: async () => {
        try {
          await ApiClient.deleteBrand(brand.brand_key);
          onRefresh();
        } catch (err: unknown) {
          const message = err instanceof Error ? err.message : String(err);
          toast.error('Erro ao excluir: ' + message);
        }
      },
    });
  };

  const handleToggleActive = async (brand: BrandItem) => {
    try {
      await ApiClient.setBrandActive(brand.brand_key, !brand.is_active);
      onRefresh();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error(`Erro ao ${brand.is_active ? 'desativar' : 'ativar'} marca: ${message}`);
    }
  };

  return (
    <div className="page-content brands-page">
      <Panel
        className="glass-card"
        title="Marcas cadastradas"
        subtitle={`${brands.length} marca(s) na base`}
        action={<Button onClick={() => setAddOpen(true)} icon={<Plus size={18} aria-hidden="true" />}>Adicionar marca</Button>}
      >
        <div className="brand-list">
          {brands.length === 0 && <EmptyState title="Nenhuma marca cadastrada" description="Adicione a primeira marca para iniciar." icon={<Globe size={20} aria-hidden="true" />} />}
          {brands.map(b => {
            return (
              <div key={b.brand_key} className="brand-item">
                <div className={`brand-info${b.is_active === false ? ' brand-info--inactive' : ''}`}>
                  <div className="brand-avatar">
                    <img
                      src={b.logo_url || `https://www.google.com/s2/favicons?domain=${b.domain}&sz=64`}
                      alt={b.brand_name}
                      onError={(e: React.SyntheticEvent<HTMLImageElement>) => { e.currentTarget.src = `https://ui-avatars.com/api/?name=${b.brand_name}&background=000000&color=ffffff`; }}
                    />
                  </div>
                  <div>
                    <div className="brand-name-row">
                      <p className="brand-name-text">{b.brand_name}</p>
                      <Status tone={b.is_active === false ? 'warning' : 'success'}>{b.is_active === false ? 'Inativa' : 'Ativa'}</Status>
                    </div>
                    <p className="brand-domain-text"><Globe size={12} /> {b.domain}</p>
                  </div>
                </div>
                <div className="brand-actions">
                  <button
                    type="button"
                    className="btn-icon brand-toggle-action"
                    onClick={() => handleToggleActive(b)}
                    aria-label={`${b.is_active !== false ? 'Desativar' : 'Ativar'} marca ${b.brand_name}`}
                    aria-pressed={b.is_active !== false}
                  >
                    <Power size={18} />
                  </button>
                  <button
                    type="button"
                    className="btn-icon text-error"
                    onClick={() => handleDeleteBrand(b)}
                    aria-label={`Excluir marca ${b.brand_name}`}
                  >
                    <Trash2 size={18} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </Panel>

      {addOpen && (
        <Dialog title="Adicionar marca" onClose={() => setAddOpen(false)} busy={savingBrand} closeOnEscape={!savingBrand} closeOnOverlay={!savingBrand}>
          <form className="form-stack" onSubmit={handleAddBrand}>
            <label className="form-group">
              <span className="label">Nome da marca</span>
              <input className="input" value={newBrand.brand_name} onChange={event => setNewBrand(value => ({ ...value, brand_name: event.target.value }))} required />
            </label>
            <label className="form-group">
              <span className="label">Identificador</span>
              <input className="input" value={newBrand.brand_key} onChange={event => setNewBrand(value => ({ ...value, brand_key: event.target.value }))} placeholder="ex.: zara" required />
            </label>
            <label className="form-group">
              <span className="label">Domínio</span>
              <input className="input" value={newBrand.domain} onChange={event => setNewBrand(value => ({ ...value, domain: event.target.value }))} placeholder="www.exemplo.com.br" required />
            </label>
            <div className="dialog-actions">
              <Button variant="outline" onClick={() => setAddOpen(false)} disabled={savingBrand}>Cancelar</Button>
              <Button type="submit" loading={savingBrand} loadingLabel="Adicionando marca">Adicionar marca</Button>
            </div>
          </form>
        </Dialog>
      )}

      <ConfirmDialog state={confirmState} onClose={() => setConfirmState(null)} cancelLabel="Manter marca" />
    </div>
  );
};
