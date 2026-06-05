import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { contactsApi } from '../api/client';
import ContactFormModal from '../components/ContactFormModal';

export default function Contactos() {
  const qc = useQueryClient();
  const [search, setSearch] = useState('');
  const [modalOpen, setModalOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ['contactos', search],
    queryFn: () => contactsApi.list(search ? { search } : undefined),
  });

  return (
    <>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Contactos</h1>
          <p className="text-sm text-gray-500 mt-0.5">Personas vinculadas a empresas</p>
        </div>
        <button
          onClick={() => setModalOpen(true)}
          className="px-4 py-2 text-sm font-semibold bg-purple-600 text-white rounded-lg hover:bg-purple-700"
        >
          + Nuevo contacto
        </button>
      </div>

      <div className="mb-4">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Buscar por nombre, email o teléfono..."
          className="w-full max-w-md px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400 bg-white"
        />
      </div>

      <div className="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-gray-400 text-sm">Cargando...</div>
        ) : !data || data.length === 0 ? (
          <div className="p-8 text-center text-gray-400 text-sm">
            {search ? 'Sin resultados' : 'No hay contactos todavía'}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr className="text-xs text-gray-500 uppercase">
                <th className="text-left px-4 py-2.5">Contacto</th>
                <th className="text-left px-4 py-2.5">Empresa</th>
                <th className="text-left px-4 py-2.5">Puesto</th>
                <th className="text-left px-4 py-2.5">Teléfono</th>
                <th className="text-left px-4 py-2.5">Email</th>
              </tr>
            </thead>
            <tbody>
              {data.map((c) => (
                <tr key={c.id} className="border-t border-gray-50 hover:bg-purple-50/40">
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-gray-800">{c.nombre_completo}</span>
                      {c.is_primary && (
                        <span className="text-[10px] font-bold uppercase tracking-wider text-purple-600">
                          Principal
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-2.5">
                    {c.account_id ? (
                      <Link
                        to={`/app/accounts/${c.account_id}`}
                        className="text-purple-700 hover:underline"
                      >
                        {c.account_nombre || '(empresa)'}
                      </Link>
                    ) : (
                      <span className="text-gray-400 italic">— sin empresa</span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-gray-600">{c.puesto || '—'}</td>
                  <td className="px-4 py-2.5 text-gray-600">{c.telefono || '—'}</td>
                  <td className="px-4 py-2.5 text-gray-600">{c.email || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <ContactFormModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onCreated={() => qc.invalidateQueries({ queryKey: ['contactos'] })}
      />
    </>
  );
}
