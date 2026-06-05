const BASE = '';

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// CS Dashboard API
export const csApi = {
  me: () => apiFetch<UserInfo>('/api/me'),
  dashboard: (periodo?: string) => apiFetch<DashboardData>(`/api/v2/cs/dashboard${periodo ? `?periodo=${periodo}` : ''}`),
  clientes: () => apiFetch<ClienteData[]>('/api/v2/cs/clientes'),
  account: (id: string, periodo?: string) => apiFetch<AccountDetail>(`/api/v2/cs/account/${id}${periodo ? `?periodo=${periodo}` : ''}`),
  mrrTrend: (params?: string) => apiFetch<TrendPoint[]>(`/cs/api/mrr-trend${params ? `?${params}` : ''}`),
  mrrTrendUN: (params?: string) => apiFetch<TrendUNPoint[]>(`/cs/api/mrr-trend-un${params ? `?${params}` : ''}`),
  operacionTrend: (params?: string) => apiFetch<OpTrendPoint[]>(`/cs/api/operacion-trend${params ? `?${params}` : ''}`),
};

// Types
export interface UserInfo {
  id: string; nombre: string; correo: string; rol: string;
}

export interface DashboardData {
  mrr_total: number; arr_total: number; num_cuentas: number;
  total_sucursales: number; facturado_periodo: number;
  pagado_periodo: number; pendiente_periodo: number;
  cat_counts: Record<string, number>;
  top_riesgo: AccountScore[];
  kam_data: KamSummary[];
  alertas: Alerta[];
  periodo_label: string;
}

export interface AccountScore {
  id: string; nombre: string; mrr: number; sucursales: number;
  unidades_contratadas: string; tier: string; giro: string;
  score: number; categoria: string; color: string;
  kam_nombre: string;
}

export interface ClienteData extends AccountScore {
  nps: number | null;
  owners: { nombre: string; correo: string }[];
}

export interface AccountDetail {
  account: AccountScore;
  health: { score: number; categoria: string; color: string; desglose: Record<string, any> };
  fact_por_un: Record<string, { facturado: number; pagado: number; pendiente: number; count: number }>;
  citas_por_un: Record<string, { total: number; terminadas: number }>;
  periodo_label: string;
}

export interface KamSummary {
  id: string; nombre: string; num_cuentas: number; mrr: number; sucursales: number;
}

export interface Alerta {
  cuenta: string; account_id: string; kam: string; tipo: string;
  titulo: string; detalle: string; severidad: string; accion: string;
}

export interface TrendPoint {
  mes: string; mes_label: string; facturado: number; pagado: number; pendiente: number;
}

export interface TrendUNPoint {
  mes: string; mes_label: string; aromatex: number; pestex: number; total: number;
}

export interface OpTrendPoint {
  mes: string; mes_label: string; total: number; terminadas: number;
  canceladas: number; no_realizadas: number; pct_cumplimiento: number;
}
