import { useEffect, useRef, useState } from 'react';
import { accountsApi, type AccountSearchHit } from '../api/client';

interface Props {
  value: { id: string; nombre: string } | null;
  onChange: (acc: { id: string; nombre: string } | null) => void;
  placeholder?: string;
  autoFocus?: boolean;
}

export default function AccountPicker({ value, onChange, placeholder, autoFocus }: Props) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<AccountSearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Debounced search
  useEffect(() => {
    if (value) return;
    const q = query.trim();
    if (q.length < 2) {
      setResults([]);
      return;
    }
    const handle = setTimeout(async () => {
      setLoading(true);
      try {
        const rows = await accountsApi.search(q);
        setResults(rows);
      } catch {
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, 200);
    return () => clearTimeout(handle);
  }, [query, value]);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  if (value) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 bg-purple-50 border border-purple-200 rounded-lg">
        <span className="text-sm font-semibold text-purple-800 flex-1 truncate">{value.nombre}</span>
        <button
          type="button"
          onClick={() => { onChange(null); setQuery(''); }}
          className="text-xs text-purple-600 hover:text-purple-800"
        >
          Cambiar
        </button>
      </div>
    );
  }

  const exactMatch = results.some(r => r.nombre.toLowerCase() === query.trim().toLowerCase());
  const canCreate = query.trim().length >= 2 && !exactMatch && !creating;

  async function handleCreate() {
    const nombre = query.trim();
    if (!nombre) return;
    setCreating(true);
    setError(null);
    try {
      const acc = await accountsApi.create({ nombre });
      onChange({ id: acc.id, nombre: acc.nombre });
      setQuery('');
      setOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'No se pudo crear');
    } finally {
      setCreating(false);
    }
  }

  return (
    <div ref={wrapRef} className="relative">
      <input
        type="text"
        value={query}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); setError(null); }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder || 'Buscar empresa...'}
        autoFocus={autoFocus}
        className="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:border-purple-400"
      />
      {open && (query.trim().length >= 2) && (
        <div className="absolute z-10 mt-1 w-full bg-white border border-gray-200 rounded-lg shadow-lg max-h-60 overflow-y-auto">
          {loading && (
            <div className="px-3 py-2 text-xs text-gray-400">Buscando...</div>
          )}
          {!loading && results.length === 0 && (
            <div className="px-3 py-2 text-xs text-gray-400">Sin resultados</div>
          )}
          {results.map((r) => (
            <button
              type="button"
              key={r.id}
              onClick={() => { onChange({ id: r.id, nombre: r.nombre }); setQuery(''); setOpen(false); }}
              className="w-full text-left px-3 py-2 text-sm hover:bg-purple-50"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-gray-800 truncate">{r.nombre}</span>
                {r.is_cliente && (
                  <span className="text-[10px] font-semibold uppercase text-green-600">Cliente</span>
                )}
              </div>
              {r.rfc && <span className="text-xs text-gray-400">{r.rfc}</span>}
            </button>
          ))}
          {canCreate && (
            <button
              type="button"
              onClick={handleCreate}
              className="w-full text-left px-3 py-2 text-sm border-t border-gray-100 hover:bg-purple-50 text-purple-700 font-semibold"
            >
              + Crear nueva: "{query.trim()}"
            </button>
          )}
        </div>
      )}
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  );
}
