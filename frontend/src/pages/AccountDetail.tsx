import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, Link } from 'react-router-dom';
import { accountsApi } from '../api/client';
import KpiCard from '../components/KpiCard';
import ContactFormModal from '../components/ContactFormModal';
import OportunidadFormModal from '../components/OportunidadFormModal';

const fmt = (n: number) => '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });

const etapaColor: Record<string, string> = {
  'Cerrado Ganado': 'bg-green-100 text-green-700',
  'Cerrado Perdido': 'bg-red-100 text-red-700',
  'Negociacion': 'bg-purple-100 text-purple-700',
  'Propuesta': 'bg-blue-100 text-blue-700',
  'Calificacion': 'bg-yellow-100 text-yellow-700',
};

export default function AccountDetail() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const [contactModalOpen, setContactModalOpen] = useState(false);
  const [oppModalOpen, setOppModalOpen] = useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: ['account', id],
    queryFn: () => accountsApi.get(id!),
    enabled: !!id,
  });

  if (isLoading) {
    return <div className="flex items-center justify-center h-64 text-gray-400">Cargando...</div>;
  }
  if (error || !data) {
    return (
      <div className="bg-white rounded-2xl border border-red-100 p-6">
        <p className="text-sm text-red-600">No se pudo cargar la empresa.</p>
      </div>
    );
  }

  return (
    <>
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <h1 className="text-2xl font-bold text-gray-900">{data.nombre}</h1>
            <span className={`px-2 py-0.5 text-xs font-semibold rounded-full ${
              data.is_cliente ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'
            }`}>
              {data.is_cliente ? 'Cliente' : 'Prospecto'}
            </span>
            {data.cs?.tier && (
              <span className="px-2 py-0.5 text-xs font-semibold rounded-full bg-amber-100 text-amber-700">
                {data.cs.tier}
              </span>
            )}
          </div>
          <p className="text-sm text-gray-500">
            {[data.nombre_comercial, data.rfc, data.industria, data.estado]
              .filter(Boolean).join(' · ')}
            {data.owner_nombre && ` · Owner: ${data.owner_nombre}`}
          </p>
        </div>
        {data.website && (
          <a href={data.website} target="_blank" rel="noreferrer"
             className="text-sm text-purple-600 hover:underline">
            {data.website.replace(/^https?:\/\//, '')} ↗
          </a>
        )}
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <KpiCard
          label="MRR"
          value={data.cs ? fmt(data.cs.mrr) : '—'}
          sub={data.cs ? `${data.cs.sucursales} sucursales` : 'Sin link a CS'}
          color={data.cs && data.cs.mrr > 0 ? 'green' : 'gray'}
        />
        <KpiCard label="Pipe Abierto" value={fmt(data.valor_pipe_abierto)} sub="MXN" />
        <KpiCard label="Ganado Total" value={fmt(data.valor_ganado_total)} sub="histórico" color="green" />
        <KpiCard
          label="Actividad"
          value={`${data.counts.leads}L · ${data.counts.oportunidades}D`}
          sub={`${data.counts.contactos} contactos · ${data.counts.cotizaciones} cotiz.`}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Contactos */}
        <Section
          title="Contactos"
          empty="Sin contactos aún"
          count={data.contactos.length}
          action={
            <button
              onClick={() => setContactModalOpen(true)}
              className="text-xs font-semibold text-purple-600 hover:text-purple-800"
            >
              + Agregar
            </button>
          }
        >
          {data.contactos.map((c) => (
            <div key={c.id} className="py-2.5 border-t border-gray-50 first:border-t-0">
              <div className="flex items-center justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <p className="font-semibold text-gray-800 text-sm">{c.nombre_completo}</p>
                    {c.is_primary && (
                      <span className="text-[10px] font-bold uppercase tracking-wider text-purple-600">
                        Principal
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500">
                    {[c.puesto, c.telefono, c.email].filter(Boolean).join(' · ') || '—'}
                  </p>
                </div>
              </div>
            </div>
          ))}
        </Section>

        {/* Oportunidades */}
        <Section
          title="Oportunidades"
          empty="Sin deals"
          count={data.oportunidades.length}
          action={
            <button
              onClick={() => setOppModalOpen(true)}
              className="text-xs font-semibold text-purple-600 hover:text-purple-800"
            >
              + Agregar
            </button>
          }
        >
          {data.oportunidades.map((o) => (
            <div key={o.id} className="py-2.5 border-t border-gray-50 first:border-t-0">
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <p className="font-semibold text-gray-800 text-sm truncate">{o.nombre}</p>
                  <p className="text-xs text-gray-500">
                    {o.marca_interes && `${o.marca_interes} · `}
                    {o.probabilidad}% · {o.fecha_cierre_esperada || 'sin fecha'}
                  </p>
                </div>
                <div className="text-right shrink-0 ml-3">
                  <p className="text-sm font-semibold text-gray-800">{fmt(o.valor)}</p>
                  <span className={`inline-block mt-0.5 px-1.5 py-0.5 text-[10px] font-semibold rounded ${
                    etapaColor[o.etapa] || 'bg-gray-100 text-gray-600'
                  }`}>
                    {o.etapa}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </Section>

        {/* Leads */}
        <Section title="Leads" empty="Sin leads" count={data.leads.length}>
          {data.leads.map((l) => (
            <div key={l.id} className="py-2.5 border-t border-gray-50 first:border-t-0">
              <div className="flex items-center justify-between">
                <div className="min-w-0">
                  <p className="font-semibold text-gray-800 text-sm truncate">
                    {l.nombre || l.telefono}
                  </p>
                  <p className="text-xs text-gray-500">
                    {[l.origen, l.marca_interes, l.usuario_asignado?.nombre]
                      .filter(Boolean).join(' · ') || '—'}
                  </p>
                </div>
                <div className="text-right shrink-0 ml-3">
                  {l.valor_estimado != null && (
                    <p className="text-xs text-gray-500">{fmt(l.valor_estimado)}</p>
                  )}
                  <span className="inline-block mt-0.5 px-1.5 py-0.5 text-[10px] font-semibold rounded bg-gray-100 text-gray-600">
                    {l.etapa_pipeline}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </Section>

        {/* Cotizaciones */}
        <Section title="Cotizaciones" empty="Sin cotizaciones" count={data.cotizaciones.length}>
          {data.cotizaciones.map((c) => (
            <div key={c.id} className="py-2.5 border-t border-gray-50 first:border-t-0">
              <div className="flex items-center justify-between">
                <div>
                  <p className="font-semibold text-gray-800 text-sm">
                    {c.folio || `#${c.id.slice(0, 8)}`}
                  </p>
                  <p className="text-xs text-gray-500">
                    {new Date(c.fecha).toLocaleDateString('es-MX')}
                    {c.marca && ` · ${c.marca}`}
                    {c.enviada_whatsapp && ' · WA ✓'}
                    {c.enviada_correo && ' · Email ✓'}
                  </p>
                </div>
                <div className="text-right">
                  <p className="text-sm font-semibold text-gray-800">{fmt(c.total)}</p>
                  {c.pdf_url && (
                    <a href={c.pdf_url} target="_blank" rel="noreferrer"
                       className="text-xs text-purple-600 hover:underline">PDF ↗</a>
                  )}
                </div>
              </div>
            </div>
          ))}
        </Section>
      </div>

      <div className="mt-6 text-xs text-gray-400">
        <Link to="/app" className="hover:text-purple-600">← Dashboard</Link>
      </div>

      <ContactFormModal
        open={contactModalOpen}
        onClose={() => setContactModalOpen(false)}
        defaultAccountId={data.id}
        defaultAccountNombre={data.nombre}
        onCreated={() => qc.invalidateQueries({ queryKey: ['account', id] })}
      />

      <OportunidadFormModal
        open={oppModalOpen}
        onClose={() => setOppModalOpen(false)}
        accountNombre={data.nombre}
        contactos={data.contactos}
        onCreated={() => qc.invalidateQueries({ queryKey: ['account', id] })}
      />
    </>
  );
}

interface SectionProps {
  title: string;
  count: number;
  empty: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}

function Section({ title, count, empty, children, action }: SectionProps) {
  return (
    <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-5">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-800">{title}</h2>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-400">{count}</span>
          {action}
        </div>
      </div>
      {count === 0 ? (
        <p className="text-xs text-gray-400 italic">{empty}</p>
      ) : (
        <div>{children}</div>
      )}
    </div>
  );
}
