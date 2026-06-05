import { useState } from 'react';
import { contactsApi, type ContactoLite } from '../api/client';
import AccountPicker from './AccountPicker';

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated?: (c: ContactoLite) => void;
  defaultAccountId?: string;
  defaultAccountNombre?: string;
}

export default function ContactFormModal({
  open, onClose, onCreated, defaultAccountId, defaultAccountNombre,
}: Props) {
  const [nombre, setNombre] = useState('');
  const [apellido, setApellido] = useState('');
  const [email, setEmail] = useState('');
  const [telefono, setTelefono] = useState('');
  const [puesto, setPuesto] = useState('');
  const [isPrimary, setIsPrimary] = useState(false);
  const [notas, setNotas] = useState('');
  const [account, setAccount] = useState<{ id: string; nombre: string } | null>(
    defaultAccountId && defaultAccountNombre
      ? { id: defaultAccountId, nombre: defaultAccountNombre }
      : null
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const accountLocked = !!defaultAccountId;

  if (!open) return null;

  function reset() {
    setNombre(''); setApellido(''); setEmail(''); setTelefono('');
    setPuesto(''); setIsPrimary(false); setNotas('');
    if (!accountLocked) setAccount(null);
    setError(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!nombre.trim()) {
      setError('El nombre es obligatorio');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const created = await contactsApi.create({
        nombre: nombre.trim(),
        apellido: apellido.trim() || undefined,
        email: email.trim() || undefined,
        telefono: telefono.trim() || undefined,
        puesto: puesto.trim() || undefined,
        is_primary: isPrimary,
        notas: notas.trim() || undefined,
        account_id: account?.id || null,
      });
      onCreated?.(created);
      reset();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo crear el contacto');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit} className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-bold text-gray-900">Nuevo contacto</h2>
            <button
              type="button" onClick={() => { reset(); onClose(); }}
              className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            >×</button>
          </div>

          <div className="space-y-3">
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Empresa</label>
              {accountLocked && account ? (
                <div className="px-3 py-2 bg-gray-50 border border-gray-200 rounded-lg text-sm text-gray-700">
                  {account.nombre}
                </div>
              ) : (
                <AccountPicker value={account} onChange={setAccount} />
              )}
              {!accountLocked && !account && (
                <p className="mt-1 text-xs text-gray-400">
                  Opcional, pero recomendado para no dejar el contacto huérfano
                </p>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Nombre *" value={nombre} onChange={setNombre} autoFocus />
              <Field label="Apellido" value={apellido} onChange={setApellido} />
            </div>

            <Field label="Puesto" value={puesto} onChange={setPuesto} />

            <div className="grid grid-cols-2 gap-3">
              <Field label="Teléfono" value={telefono} onChange={setTelefono} type="tel" />
              <Field label="Email" value={email} onChange={setEmail} type="email" />
            </div>

            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">Notas</label>
              <textarea
                value={notas} onChange={(e) => setNotas(e.target.value)} rows={2}
                className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400"
              />
            </div>

            <label className="flex items-center gap-2 text-sm text-gray-700">
              <input
                type="checkbox" checked={isPrimary} onChange={(e) => setIsPrimary(e.target.checked)}
                className="rounded"
              />
              Marcar como contacto principal de la empresa
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
              {submitting ? 'Guardando...' : 'Crear contacto'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface FieldProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type?: string;
  autoFocus?: boolean;
}

function Field({ label, value, onChange, type = 'text', autoFocus }: FieldProps) {
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
