import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { accountsApi } from '../api/client';
import AccountFormModal from '../components/AccountFormModal';
import ImportAccountsModal from '../components/ImportAccountsModal';

export default function Empresas() {
  const qc = useQueryClient();
  const [search, setSearch] = useState('');
  const [modalOpen, setModalOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ['empresas', search],
    queryFn: () => accountsApi.list(search || undefined),
  });

  return (
    <>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Empresas</h1>
          <p className="text-sm text-gray-500 mt-0.5">Clientes y prospectos</p>
        </div>
        <div className="flex items-center gap-2">
          <a
            href={accountsApi.exportUrl(search)}
            className="px-3 py-2 text-sm font-medium text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50"
            download
          >
            ↓ Exportar CSV
          </a>
          <button
            onClick={() => setImportOpen(true)}
            className="px-3 py-2 text-sm font-medium text-gray-700 border border-gray-300 rounded-lg hover:bg-gray-50"
          >
            ↑ Importar CSV
          </button>
          <button
            onClick={() => setModalOpen(true)}
            className="px-4 py-2 text-sm font-semibold bg-purple-600 text-white rounded-lg hover:bg-purple-700"
          >
            + Nueva empresa
          </button>
        </div>
      </div>

      <div className="mb-4">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Buscar por ID, nombre, RFC o nombre comercial..."
          className="w-full max-w-md px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400 bg-white"
        />
      </div>

      <div className="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-gray-400 text-sm">Cargando...</div>
        ) : !data || data.length === 0 ? (
          <div className="p-8 text-center text-gray-400 text-sm">
            {search ? 'Sin resultados' : 'No hay empresas todavía'}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr className="text-xs text-gray-500 uppercase">
                <th className="text-left px-4 py-2.5">ID</th>
                <th className="text-left px-4 py-2.5">Empresa</th>
                <th className="text-left px-4 py-2.5">RFC</th>
                <th className="text-left px-4 py-2.5">Industria</th>
                <th className="text-left px-4 py-2.5">Estado</th>
                <th className="text-center px-4 py-2.5">Tipo</th>
              </tr>
            </thead>
            <tbody>
              {data.map((acc) => (
                <tr key={acc.id} className="border-t border-gray-50 hover:bg-purple-50/40">
                  <td className="px-4 py-2.5">
                    <span className="font-mono text-xs text-gray-500">{acc.client_id || '—'}</span>
                  </td>
                  <td className="px-4 py-2.5">
                    <Link
                      to={`/app/accounts/${acc.id}`}
                      className="font-semibold text-purple-700 hover:underline"
                    >
                      {acc.nombre}
                    </Link>
                    {acc.nombre_comercial && (
                      <div className="text-xs text-gray-400">{acc.nombre_comercial}</div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-gray-600">{acc.rfc || '—'}</td>
                  <td className="px-4 py-2.5 text-gray-600">{acc.industria || '—'}</td>
                  <td className="px-4 py-2.5 text-gray-600">{acc.estado || '—'}</td>
                  <td className="px-4 py-2.5 text-center">
                    <span className={`px-2 py-0.5 text-xs font-semibold rounded-full ${
                      acc.is_cliente ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'
                    }`}>
                      {acc.is_cliente ? 'Cliente' : 'Prospecto'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <AccountFormModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onCreated={() => qc.invalidateQueries({ queryKey: ['empresas'] })}
      />

      <ImportAccountsModal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onCompleted={() => qc.invalidateQueries({ queryKey: ['empresas'] })}
      />
    </>
  );
}
