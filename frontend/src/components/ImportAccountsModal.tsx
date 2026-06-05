import { useState } from 'react';
import { accountsApi } from '../api/client';

interface Props {
  open: boolean;
  onClose: () => void;
  onCompleted?: () => void;
}

type ImportResult = {
  created: number;
  skipped: number;
  failed: { row: number; error: string }[];
};

export default function ImportAccountsModal({ open, onClose, onCompleted }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [markAll, setMarkAll] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResult | null>(null);

  if (!open) return null;

  function reset() {
    setFile(null); setMarkAll(false); setError(null); setResult(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) { setError('Elegí un archivo CSV'); return; }
    setSubmitting(true); setError(null); setResult(null);
    try {
      const res = await accountsApi.importCsv(file, markAll);
      setResult(res);
      onCompleted?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error en la importación');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit} className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold text-gray-900">Importar empresas</h2>
            <button
              type="button" onClick={() => { reset(); onClose(); }}
              className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            >×</button>
          </div>

          <div className="space-y-4">
            <div className="bg-blue-50 border border-blue-100 rounded-lg p-3 text-xs text-blue-800 space-y-1">
              <p className="font-semibold">Formato esperado (CSV en UTF-8):</p>
              <p className="font-mono text-[11px] bg-white border border-blue-200 rounded p-1.5 overflow-x-auto">
                nombre,rfc,nombre_comercial,industria,tamano,num_sucursales,telefono,website,ciudad,estado,pais,is_cliente,notas
              </p>
              <p>Solo <b>nombre</b> es obligatorio. El sistema asigna el ID (EMP-XXXX) automáticamente.</p>
              <p>Duplicados (mismo RFC o mismo nombre exacto) se omiten.</p>
            </div>

            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Archivo CSV</label>
              <input
                type="file" accept=".csv,text/csv"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
                className="block w-full text-sm text-gray-600 file:mr-3 file:py-2 file:px-3 file:rounded-lg file:border-0 file:bg-purple-100 file:text-purple-700 file:font-semibold hover:file:bg-purple-200"
              />
              {file && (
                <p className="mt-1 text-xs text-gray-500">{file.name} ({(file.size / 1024).toFixed(1)} KB)</p>
              )}
            </div>

            <label className="flex items-center gap-2 text-sm text-gray-700">
              <input
                type="checkbox" checked={markAll}
                onChange={(e) => setMarkAll(e.target.checked)} className="rounded"
              />
              Marcar todas las empresas importadas como <b>Cliente</b>
            </label>

            {error && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-2 text-xs text-red-700">{error}</div>
            )}

            {result && (
              <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 text-sm space-y-2">
                <div className="flex items-center gap-4">
                  <Stat label="Creadas" value={result.created} color="green" />
                  <Stat label="Omitidas (dup)" value={result.skipped} color="gray" />
                  <Stat label="Errores" value={result.failed.length} color={result.failed.length > 0 ? 'red' : 'gray'} />
                </div>
                {result.failed.length > 0 && (
                  <div>
                    <p className="text-xs font-semibold text-gray-700 mb-1">Filas con error:</p>
                    <ul className="text-xs text-red-700 max-h-32 overflow-y-auto space-y-0.5">
                      {result.failed.slice(0, 50).map((f) => (
                        <li key={f.row}>línea {f.row}: {f.error}</li>
                      ))}
                      {result.failed.length > 50 && (
                        <li className="text-gray-500 italic">... y {result.failed.length - 50} más</li>
                      )}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 mt-6">
            <button
              type="button" onClick={() => { reset(); onClose(); }}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
            >
              {result ? 'Cerrar' : 'Cancelar'}
            </button>
            {!result && (
              <button
                type="submit" disabled={submitting || !file}
                className="px-4 py-2 text-sm font-semibold bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50"
              >
                {submitting ? 'Importando...' : 'Importar'}
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: number; color: 'green' | 'red' | 'gray' }) {
  const colors = {
    green: 'text-green-700', red: 'text-red-700', gray: 'text-gray-600',
  };
  return (
    <div>
      <div className={`text-2xl font-bold ${colors[color]}`}>{value}</div>
      <div className="text-[10px] uppercase text-gray-500 font-semibold">{label}</div>
    </div>
  );
}
