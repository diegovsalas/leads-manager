import { useState } from 'react';
import { oportunidadesApi, type ContactoLite, type OportunidadLite } from '../api/client';

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated?: (o: OportunidadLite) => void;
  accountNombre: string;
  contactos: ContactoLite[];
}

const etapas = ['Calificación', 'Análisis', 'Propuesta', 'Negociación', 'Cerrado Ganado', 'Cerrado Perdido'];
const marcas = ['', 'Aromatex', 'Pestex', 'Weldex'];
const saleTypes = ['', 'suscripcion_nueva', 'servicio_unico', 'upsell'];
const probabilidadPorEtapa: Record<string, number> = {
  'Calificación': 10, 'Análisis': 25, 'Propuesta': 50,
  'Negociación': 75, 'Cerrado Ganado': 100, 'Cerrado Perdido': 0,
};

export default function OportunidadFormModal({
  open, onClose, onCreated, accountNombre, contactos,
}: Props) {
  const [nombre, setNombre] = useState('');
  const [valor, setValor] = useState('');
  const [marcaInteres, setMarcaInteres] = useState('');
  const [etapa, setEtapa] = useState('Calificación');
  const [probabilidad, setProbabilidad] = useState('10');
  const [fechaCierre, setFechaCierre] = useState('');
  const [numSucursales, setNumSucursales] = useState('');
  const [saleType, setSaleType] = useState('');
  const [monthlyAmount, setMonthlyAmount] = useState('');
  const [contactoId, setContactoId] = useState('');
  const [notas, setNotas] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  function reset() {
    setNombre(''); setValor(''); setMarcaInteres(''); setEtapa('Calificación');
    setProbabilidad('10'); setFechaCierre(''); setNumSucursales('');
    setSaleType(''); setMonthlyAmount(''); setContactoId(''); setNotas('');
    setError(null);
  }

  function onEtapaChange(v: string) {
    setEtapa(v);
    setProbabilidad(String(probabilidadPorEtapa[v] ?? 10));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!nombre.trim()) {
      setError('El nombre de la oportunidad es obligatorio');
      return;
    }
    if (!marcaInteres) {
      setError('Marca de interés es obligatoria (Aromatex/Pestex/Weldex)');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const contacto = contactos.find((c) => c.id === contactoId);
      const created = await oportunidadesApi.create({
        nombre: nombre.trim(),
        empresa: accountNombre,
        valor: valor ? parseFloat(valor) : 0,
        moneda: 'MXN',
        marca_interes: marcaInteres,
        etapa,
        probabilidad: parseInt(probabilidad) || 0,
        fecha_cierre_esperada: fechaCierre || null,
        num_sucursales: numSucursales ? parseInt(numSucursales) : undefined,
        sale_type: saleType || undefined,
        monthly_amount: monthlyAmount ? parseFloat(monthlyAmount) : undefined,
        contacto_nombre: contacto?.nombre_completo || undefined,
        contacto_telefono: contacto?.telefono || undefined,
        contacto_email: contacto?.email || undefined,
        notas: notas.trim() || undefined,
      });
      onCreated?.(created);
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo crear la oportunidad');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit} className="p-6">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="text-lg font-bold text-gray-900">Nueva oportunidad</h2>
              <p className="text-xs text-gray-500">{accountNombre}</p>
            </div>
            <button
              type="button" onClick={() => { reset(); onClose(); }}
              className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            >×</button>
          </div>

          <div className="space-y-3">
            <Field
              label="Nombre de la oportunidad *"
              value={nombre} onChange={setNombre} autoFocus
              placeholder={`${accountNombre} - ...`}
            />

            <div className="grid grid-cols-2 gap-3">
              <Select
                label="Marca de interés *"
                value={marcaInteres} onChange={setMarcaInteres}
                options={marcas}
              />
              <Field
                label="Valor estimado (MXN)"
                value={valor} onChange={setValor} type="number"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Select label="Etapa" value={etapa} onChange={onEtapaChange} options={etapas} />
              <Field
                label="Probabilidad %" value={probabilidad}
                onChange={setProbabilidad} type="number"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field
                label="Fecha cierre estimada" value={fechaCierre}
                onChange={setFechaCierre} type="date"
              />
              <Field
                label="# Sucursales" value={numSucursales}
                onChange={setNumSucursales} type="number"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Select label="Tipo de venta" value={saleType} onChange={setSaleType} options={saleTypes} />
              <Field
                label="Monto mensual (suscripción)"
                value={monthlyAmount} onChange={setMonthlyAmount} type="number"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Contacto principal</label>
              <select
                value={contactoId} onChange={(e) => setContactoId(e.target.value)}
                className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400 bg-white"
              >
                <option value="">— Sin contacto específico —</option>
                {contactos.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.nombre_completo}{c.puesto ? ` (${c.puesto})` : ''}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Notas</label>
              <textarea
                value={notas} onChange={(e) => setNotas(e.target.value)} rows={2}
                className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400"
              />
            </div>

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
              {submitting ? 'Guardando...' : 'Crear oportunidad'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({
  label, value, onChange, type = 'text', autoFocus, placeholder,
}: {
  label: string; value: string; onChange: (v: string) => void;
  type?: string; autoFocus?: boolean; placeholder?: string;
}) {
  return (
    <div>
      <label className="block text-xs font-semibold text-gray-600 mb-1">{label}</label>
      <input
        type={type} value={value} onChange={(e) => onChange(e.target.value)}
        autoFocus={autoFocus} placeholder={placeholder}
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
