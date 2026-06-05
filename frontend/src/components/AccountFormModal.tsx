import { useState } from 'react';
import { accountsApi, type AccountSearchHit } from '../api/client';

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated?: (a: AccountSearchHit) => void;
}

const industrias = [
  '', 'Educación', 'Retail', 'Hotelería', 'Restaurantes', 'Industrial',
  'Salud', 'Oficinas', 'Gobierno', 'Inmobiliaria', 'Otro',
];
const tamanos = ['', 'micro', 'pequena', 'mediana', 'grande'];

export default function AccountFormModal({ open, onClose, onCreated }: Props) {
  const [nombre, setNombre] = useState('');
  const [rfc, setRfc] = useState('');
  const [industria, setIndustria] = useState('');
  const [tamano, setTamano] = useState('');
  const [telefono, setTelefono] = useState('');
  const [website, setWebsite] = useState('');
  const [estado, setEstado] = useState('');
  const [isCliente, setIsCliente] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  function reset() {
    setNombre(''); setRfc(''); setIndustria(''); setTamano('');
    setTelefono(''); setWebsite(''); setEstado(''); setIsCliente(false);
    setError(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!nombre.trim()) {
      setError('El nombre de la empresa es obligatorio');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const acc = await accountsApi.create({
        nombre: nombre.trim(),
        rfc: rfc.trim() || undefined,
        industria: industria || undefined,
        tamano: tamano || undefined,
        telefono: telefono.trim() || undefined,
        website: website.trim() || undefined,
        estado: estado.trim() || undefined,
        is_cliente: isCliente,
      });
      onCreated?.(acc);
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo crear la empresa');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit} className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold text-gray-900">Nueva empresa</h2>
            <button
              type="button" onClick={() => { reset(); onClose(); }}
              className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            >×</button>
          </div>

          <div className="space-y-3">
            <Field label="Nombre *" value={nombre} onChange={setNombre} autoFocus />
            <div className="grid grid-cols-2 gap-3">
              <Field label="RFC" value={rfc} onChange={(v) => setRfc(v.toUpperCase())} />
              <Field label="Teléfono" value={telefono} onChange={setTelefono} type="tel" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Select label="Industria" value={industria} onChange={setIndustria} options={industrias} />
              <Select label="Tamaño" value={tamano} onChange={setTamano} options={tamanos} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Estado" value={estado} onChange={setEstado} />
              <Field label="Website" value={website} onChange={setWebsite} />
            </div>
            <label className="flex items-center gap-2 text-sm text-gray-700">
              <input
                type="checkbox" checked={isCliente}
                onChange={(e) => setIsCliente(e.target.checked)} className="rounded"
              />
              Ya es cliente (ya cerró venta)
            </label>

            {error && (
              <div className="bg-red-50 border border-red-200 rounded-lg p-2 text-xs text-red-700">{error}</div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 mt-6">
            <button
              type="button" onClick={() => { reset(); onClose(); }}
              className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
            >Cancelar</button>
            <button
              type="submit" disabled={submitting}
              className="px-4 py-2 text-sm font-semibold bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50"
            >
              {submitting ? 'Guardando...' : 'Crear empresa'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({
  label, value, onChange, type = 'text', autoFocus,
}: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; autoFocus?: boolean;
}) {
  return (
    <div>
      <label className="block text-xs font-semibold text-gray-600 mb-1">{label}</label>
      <input
        type={type} value={value} onChange={(e) => onChange(e.target.value)}
        autoFocus={autoFocus}
        className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400"
      />
    </div>
  );
}

function Select({
  label, value, onChange, options,
}: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <div>
      <label className="block text-xs font-semibold text-gray-600 mb-1">{label}</label>
      <select
        value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400 bg-white"
      >
        {options.map((o) => <option key={o} value={o}>{o || '—'}</option>)}
      </select>
    </div>
  );
}
