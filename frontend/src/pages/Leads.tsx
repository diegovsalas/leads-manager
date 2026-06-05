import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  leadsApi, accountsApi, contactsApi,
  type LeadLite, type ContactoLite,
} from '../api/client';
import LeadEditModal from '../components/LeadEditModal';

const etapaColor: Record<string, string> = {
  'Nuevo lead': 'bg-blue-100 text-blue-700',
  'Contactado': 'bg-indigo-100 text-indigo-700',
  'Cotización': 'bg-yellow-100 text-yellow-700',
  'Demo': 'bg-purple-100 text-purple-700',
  'Negociación': 'bg-orange-100 text-orange-700',
  'Cerrado Ganado': 'bg-green-100 text-green-700',
  'Cerrado Perdido': 'bg-red-100 text-red-700',
};

export default function Leads() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<LeadLite | null>(null);
  const [currentAccount, setCurrentAccount] = useState<{ id: string; nombre: string } | null>(null);
  const [currentContact, setCurrentContact] = useState<ContactoLite | null>(null);
  const [search, setSearch] = useState('');

  const { data: leads, isLoading } = useQuery({
    queryKey: ['leads'],
    queryFn: () => leadsApi.list(),
  });

  async function handleRowClick(lead: LeadLite) {
    setSelected(lead);
    // Resolve linked account/contact for prefill
    const accPromise = lead.account_id
      ? accountsApi.get(lead.account_id).then((a) => ({ id: a.id, nombre: a.nombre })).catch(() => null)
      : Promise.resolve(null);
    const contactPromise = lead.contact_id && lead.account_id
      ? contactsApi.list({ accountId: lead.account_id })
          .then((cs) => cs.find((c) => c.id === lead.contact_id) || null)
          .catch(() => null)
      : Promise.resolve(null);
    const [acc, ct] = await Promise.all([accPromise, contactPromise]);
    setCurrentAccount(acc);
    setCurrentContact(ct);
  }

  const filtered = leads?.filter((l) => {
    if (!search) return true;
    const s = search.toLowerCase();
    return (
      l.nombre?.toLowerCase().includes(s) ||
      l.telefono.includes(s) ||
      l.empresa_nombre?.toLowerCase().includes(s) ||
      l.marca_interes?.toLowerCase().includes(s)
    );
  });

  return (
    <>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Leads</h1>
          <p className="text-sm text-gray-500 mt-0.5">Entradas del bot WhatsApp, Meta Ads y otras fuentes</p>
        </div>
      </div>

      <div className="mb-4">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Buscar por nombre, teléfono, empresa o marca..."
          className="w-full max-w-md px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400 bg-white"
        />
      </div>

      <div className="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-gray-400 text-sm">Cargando...</div>
        ) : !filtered || filtered.length === 0 ? (
          <div className="p-8 text-center text-gray-400 text-sm">
            {search ? 'Sin resultados' : 'No hay leads todavía'}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr className="text-xs text-gray-500 uppercase">
                <th className="text-left px-4 py-2.5">Lead</th>
                <th className="text-left px-4 py-2.5">Empresa</th>
                <th className="text-left px-4 py-2.5">Origen</th>
                <th className="text-left px-4 py-2.5">Marca</th>
                <th className="text-center px-4 py-2.5">Etapa</th>
                <th className="text-left px-4 py-2.5">Vendedor</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((l) => {
                const linked = !!l.account_id;
                return (
                  <tr
                    key={l.id}
                    onClick={() => handleRowClick(l)}
                    className="border-t border-gray-50 hover:bg-purple-50/40 cursor-pointer"
                  >
                    <td className="px-4 py-2.5">
                      <div className="font-semibold text-gray-800">{l.nombre || '(sin nombre)'}</div>
                      <div className="text-xs text-gray-400">{l.telefono}</div>
                    </td>
                    <td className="px-4 py-2.5">
                      <span className={linked ? 'text-purple-700 font-medium' : 'text-gray-500 italic'}>
                        {l.empresa_nombre || (linked ? '(ligada)' : '— sin ligar')}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-gray-600">{l.origen || '—'}</td>
                    <td className="px-4 py-2.5 text-gray-600">{l.marca_interes || '—'}</td>
                    <td className="px-4 py-2.5 text-center">
                      <span className={`px-2 py-0.5 text-xs font-semibold rounded ${
                        etapaColor[l.etapa_pipeline] || 'bg-gray-100 text-gray-600'
                      }`}>
                        {l.etapa_pipeline}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-gray-600">
                      {l.usuario_asignado?.nombre || '—'}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <LeadEditModal
        open={!!selected}
        onClose={() => { setSelected(null); setCurrentAccount(null); setCurrentContact(null); }}
        lead={selected}
        currentAccount={currentAccount}
        currentContact={currentContact}
        onChanged={() => qc.invalidateQueries({ queryKey: ['leads'] })}
      />
    </>
  );
}
