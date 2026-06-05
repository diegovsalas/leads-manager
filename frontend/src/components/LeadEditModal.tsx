import { useEffect, useState } from 'react';
import {
  contactsApi, leadsApi, oportunidadesApi,
  type ContactoLite, type LeadLite,
} from '../api/client';
import AccountPicker from './AccountPicker';

interface Props {
  open: boolean;
  onClose: () => void;
  lead: LeadLite | null;
  /** Account currently linked to the lead (if any) for prefill */
  currentAccount: { id: string; nombre: string } | null;
  /** Contact currently linked to the lead (if any) for prefill */
  currentContact: ContactoLite | null;
  onChanged?: () => void;
}

export default function LeadEditModal({
  open, onClose, lead, currentAccount, currentContact, onChanged,
}: Props) {
  const [account, setAccount] = useState<{ id: string; nombre: string } | null>(currentAccount);
  const [contacts, setContacts] = useState<ContactoLite[]>([]);
  const [contactId, setContactId] = useState<string>(currentContact?.id || '');
  const [saving, setSaving] = useState(false);
  const [converting, setConverting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setAccount(currentAccount);
      setContactId(currentContact?.id || '');
      setError(null);
    }
  }, [open, currentAccount, currentContact]);

  useEffect(() => {
    if (!account) {
      setContacts([]);
      return;
    }
    contactsApi.list({ accountId: account.id }).then(setContacts).catch(() => setContacts([]));
  }, [account]);

  if (!open || !lead) return null;

  async function handleSaveLink() {
    if (!lead) return;
    setSaving(true);
    setError(null);
    try {
      await leadsApi.update(lead.id, {
        account_id: account?.id || null,
        contact_id: contactId || null,
      });
      onChanged?.();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo guardar');
    } finally {
      setSaving(false);
    }
  }

  async function handleConvert() {
    if (!lead) return;
    if (!confirm('¿Convertir este Lead en una Oportunidad? Esto crea un deal en etapa Calificación ligado a este Lead.')) {
      return;
    }
    setConverting(true);
    setError(null);
    try {
      // Save link first if changed, then convert
      if ((account?.id || null) !== (currentAccount?.id || null) ||
          (contactId || null) !== (currentContact?.id || null)) {
        await leadsApi.update(lead.id, {
          account_id: account?.id || null,
          contact_id: contactId || null,
        });
      }
      await oportunidadesApi.fromLead(lead.id);
      onChanged?.();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo convertir');
    } finally {
      setConverting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="p-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-lg font-bold text-gray-900">Lead</h2>
              <p className="text-xs text-gray-500">
                {lead.nombre || '(sin nombre)'} · {lead.telefono}
                {lead.origen && ` · ${lead.origen}`}
              </p>
            </div>
            <button
              type="button" onClick={onClose}
              className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            >×</button>
          </div>

          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 p-3 bg-gray-50 rounded-lg text-xs">
              <Info label="Etapa" value={lead.etapa_pipeline} />
              <Info label="Marca" value={lead.marca_interes || '—'} />
              <Info label="ICP" value={lead.icp_nivel ? `${lead.icp_nivel} (${lead.icp_score})` : '—'} />
              <Info label="Vendedor" value={lead.usuario_asignado?.nombre || '—'} />
            </div>

            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Empresa</label>
              <AccountPicker value={account} onChange={(a) => { setAccount(a); setContactId(''); }} />
            </div>

            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Contacto</label>
              {!account ? (
                <div className="px-3 py-2 text-xs text-gray-400 italic bg-gray-50 rounded-lg">
                  Elegí primero una empresa
                </div>
              ) : (
                <select
                  value={contactId} onChange={(e) => setContactId(e.target.value)}
                  className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400 bg-white"
                >
                  <option value="">— Sin asignar —</option>
                  {contacts.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.nombre_completo}{c.puesto ? ` (${c.puesto})` : ''}
                    </option>
                  ))}
                </select>
              )}
              {account && contacts.length === 0 && (
                <p className="mt-1 text-xs text-gray-400">
                  Esta empresa no tiene contactos. Podés ir a la página de la empresa para agregar uno.
                </p>
              )}
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-2 text-xs text-red-700">{error}</div>
            )}
          </div>

          <div className="flex items-center justify-between gap-2 mt-6">
            <button
              type="button" onClick={handleConvert} disabled={converting || saving}
              className="px-4 py-2 text-sm font-semibold bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
            >
              {converting ? 'Convirtiendo...' : '→ Convertir a Oportunidad'}
            </button>
            <div className="flex gap-2">
              <button
                type="button" onClick={onClose}
                className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
              >Cancelar</button>
              <button
                type="button" onClick={handleSaveLink} disabled={saving || converting}
                className="px-4 py-2 text-sm font-semibold bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50"
              >
                {saving ? 'Guardando...' : 'Guardar vínculos'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase text-gray-400 font-semibold">{label}</div>
      <div className="text-gray-700">{value}</div>
    </div>
  );
}
