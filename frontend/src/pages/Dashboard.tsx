import { useQuery } from '@tanstack/react-query';
import { useSearchParams, Link } from 'react-router-dom';
import { csApi } from '../api/client';
import KpiCard from '../components/KpiCard';
import HealthBadge from '../components/HealthBadge';

const periodos = [
  { value: '2026-Q1', label: 'Q1 2026' },
  { value: '2026-Q2', label: 'Q2 2026' },
  { value: '2026-01', label: 'Ene 2026' },
  { value: '2026-02', label: 'Feb 2026' },
  { value: '2026-03', label: 'Mar 2026' },
  { value: '2026-04', label: 'Abr 2026' },
  { value: '2026-05', label: 'May 2026' },
  { value: 'all', label: 'Todo' },
];

const fmt = (n: number) => '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });

export default function Dashboard() {
  const [params, setParams] = useSearchParams();
  const periodo = params.get('periodo') || '2026-Q1';

  const { data, isLoading } = useQuery({
    queryKey: ['cs-dashboard', periodo],
    queryFn: () => csApi.dashboard(periodo),
  });

  if (isLoading || !data) {
    return <div className="flex items-center justify-center h-64 text-gray-400">Cargando...</div>;
  }

  return (
    <>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-0.5">Customer Success · {data.periodo_label}</p>
        </div>
        <select
          value={periodo}
          onChange={(e) => setParams({ periodo: e.target.value })}
          className="text-sm border border-gray-300 rounded-lg px-3 py-1.5 bg-white"
        >
          {periodos.map((p) => (
            <option key={p.value} value={p.value}>{p.label}</option>
          ))}
        </select>
      </div>

      {/* Alertas */}
      {data.alertas.length > 0 && (
        <div className="bg-white rounded-2xl border border-red-100 shadow-sm p-5 mb-6">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <span className="w-2.5 h-2.5 rounded-full bg-red-500 animate-pulse" />
              <span className="text-sm font-semibold text-gray-800">{data.alertas.length} Alertas</span>
            </div>
            <Link to="/app/alertas" className="text-xs text-purple-600 font-semibold hover:underline">Ver todas →</Link>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            {data.alertas.slice(0, 6).map((a, i) => (
              <div key={i} className={`rounded-xl p-3 border-l-4 bg-gray-50 ${
                a.severidad === 'critica' ? 'border-red-500' : a.severidad === 'alta' ? 'border-orange-400' : 'border-yellow-400'
              }`}>
                <Link to={`/app/clientes`} className="text-xs font-semibold text-purple-600 hover:underline">{a.cuenta}</Link>
                <p className="text-xs text-gray-500 mt-0.5">{a.titulo}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <KpiCard label="MRR Total" value={fmt(data.mrr_total)} sub="MXN / mes" />
        <KpiCard label="ARR Proyectado" value={fmt(data.arr_total)} sub="MXN / año" />
        <KpiCard label="Cuentas" value={data.num_cuentas} sub="bajo gestión" />
        <KpiCard label="Sucursales" value={data.total_sucursales.toLocaleString()} sub="activas" />
      </div>

      {/* Facturación periodo */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <KpiCard label="Facturado" value={fmt(data.facturado_periodo)} sub={data.periodo_label} color="gray" />
        <KpiCard label="Pagado" value={fmt(data.pagado_periodo)} sub={data.periodo_label} color="green" />
        <KpiCard label="Pendiente" value={fmt(data.pendiente_periodo)} sub={data.periodo_label} color={data.pendiente_periodo > 0 ? 'red' : 'gray'} />
      </div>

      {/* Semáforo + Top riesgo */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-6">
        <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-5">
          <h2 className="text-sm font-semibold text-gray-800 mb-4">Salud del Portafolio</h2>
          {(['Sana', 'Atención', 'Riesgo'] as const).map((cat) => {
            const color = cat === 'Sana' ? 'green' : cat === 'Atención' ? 'yellow' : 'red';
            return (
              <div key={cat} className="flex items-center justify-between py-2">
                <div className="flex items-center gap-2">
                  <span className={`w-3 h-3 rounded-full bg-${color}-500`} />
                  <span className="text-sm text-gray-600">{cat}</span>
                </div>
                <span className={`text-lg font-bold text-${color}-600`}>{data.cat_counts[cat] || 0}</span>
              </div>
            );
          })}
        </div>

        <div className="lg:col-span-2 bg-white rounded-2xl shadow-sm border border-gray-100 p-5">
          <h2 className="text-sm font-semibold text-gray-800 mb-4">Top 5 Cuentas en Riesgo</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 uppercase">
                <th className="text-left pb-3">Cuenta</th>
                <th className="text-left pb-3">KAM</th>
                <th className="text-right pb-3">MRR</th>
                <th className="text-right pb-3">Score</th>
                <th className="text-center pb-3">Estado</th>
              </tr>
            </thead>
            <tbody>
              {data.top_riesgo.map((item) => (
                <tr key={item.id} className="border-t border-gray-50">
                  <td className="py-3"><Link to={`/app/clientes`} className="font-semibold text-purple-600 hover:underline">{item.nombre}</Link></td>
                  <td className="text-gray-500">{item.kam_nombre?.split(' ')[0]}</td>
                  <td className="text-right font-medium">{fmt(item.mrr)}</td>
                  <td className="text-right"><span className={`font-bold ${item.score >= 70 ? 'text-green-600' : item.score >= 40 ? 'text-yellow-500' : 'text-red-600'}`}>{item.score}</span></td>
                  <td className="text-center"><HealthBadge score={item.score} categoria={item.categoria} color={item.color} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* KAM data */}
      {data.kam_data.length > 0 && (
        <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-5 mb-6">
          <h2 className="text-sm font-semibold text-gray-800 mb-4">Detalle por KAM</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 uppercase">
                <th className="text-left pb-3">KAM</th>
                <th className="text-right pb-3">Cuentas</th>
                <th className="text-right pb-3">Sucursales</th>
                <th className="text-right pb-3">MRR</th>
              </tr>
            </thead>
            <tbody>
              {data.kam_data.map((k) => (
                <tr key={k.id} className="border-t border-gray-50">
                  <td className="py-3 font-semibold text-purple-600">{k.nombre}</td>
                  <td className="text-right text-gray-600">{k.num_cuentas}</td>
                  <td className="text-right text-gray-600">{k.sucursales.toLocaleString()}</td>
                  <td className="text-right font-semibold">{fmt(k.mrr)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
