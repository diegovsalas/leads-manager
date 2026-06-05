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

// Accounts (empresas / Zoho-style) API
export const accountsApi = {
  list: (search?: string) =>
    apiFetch<AccountFull[]>(`/api/accounts/${search ? `?search=${encodeURIComponent(search)}` : ''}`),
  search: (q: string) =>
    apiFetch<AccountSearchHit[]>(`/api/accounts/search?q=${encodeURIComponent(q)}`),
  get: (id: string) => apiFetch<AccountFull>(`/api/accounts/${id}`),
  create: (data: {
    nombre: string; rfc?: string; nombre_comercial?: string;
    industria?: string; tamano?: string; num_sucursales?: number;
    website?: string; telefono?: string; direccion?: string;
    ciudad?: string; estado?: string; is_cliente?: boolean; notas?: string;
  }) =>
    apiFetch<AccountSearchHit>('/api/accounts/', {
      method: 'POST', body: JSON.stringify(data),
    }),
};

export const contactsApi = {
  list: (params?: { accountId?: string; search?: string }) => {
    const qs: string[] = [];
    if (params?.accountId) qs.push(`account_id=${params.accountId}`);
    if (params?.search) qs.push(`search=${encodeURIComponent(params.search)}`);
    const tail = qs.length ? `?${qs.join('&')}` : '';
    return apiFetch<ContactoLite[]>(`/api/contacts/${tail}`);
  },
  create: (data: {
    nombre: string; apellido?: string; email?: string; telefono?: string;
    whatsapp?: string; puesto?: string; departamento?: string; linkedin?: string;
    account_id?: string | null; is_primary?: boolean; notas?: string;
  }) =>
    apiFetch<ContactoLite>('/api/contacts/', {
      method: 'POST', body: JSON.stringify(data),
    }),
};

export const oportunidadesApi = {
  create: (data: {
    nombre: string; valor?: number; moneda?: string;
    marca_interes?: string; etapa?: string; probabilidad?: number;
    fecha_cierre_esperada?: string | null;
    contacto_nombre?: string; contacto_telefono?: string; contacto_email?: string;
    num_sucursales?: number; sale_type?: string; monthly_amount?: number;
    notas?: string; empresa?: string; lead_id?: string;
  }) =>
    apiFetch<OportunidadLite>('/api/oportunidades/', {
      method: 'POST', body: JSON.stringify(data),
    }),
  fromLead: (leadId: string, data?: Record<string, unknown>) =>
    apiFetch<OportunidadLite>(`/api/oportunidades/from-lead/${leadId}`, {
      method: 'POST', body: JSON.stringify(data || {}),
    }),
};

export const leadsApi = {
  list: () => apiFetch<LeadLite[]>('/api/leads/'),
  update: (id: string, data: {
    account_id?: string | null; contact_id?: string | null;
    nombre?: string; telefono?: string; marca_interes?: string;
    notas?: string; etapa_pipeline?: string;
  }) =>
    apiFetch<LeadLite>(`/api/leads/${id}`, {
      method: 'PUT', body: JSON.stringify(data),
    }),
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

// ── Accounts (empresas) ──────────────────────────────
export interface AccountSearchHit {
  id: string; nombre: string; rfc: string | null;
  nombre_comercial: string | null; is_cliente: boolean;
}

export interface ContactoLite {
  id: string; nombre: string; apellido: string | null;
  nombre_completo: string; email: string | null; telefono: string | null;
  whatsapp: string | null; puesto: string | null;
  is_primary: boolean; account_id: string | null;
  account_nombre?: string | null;
}

export interface OportunidadLite {
  id: string; nombre: string; empresa: string | null;
  valor: number; moneda: string; etapa: string;
  probabilidad: number; fecha_cierre_esperada: string | null;
  marca_interes: string | null; sale_type: string | null;
  contacto_nombre: string | null; contacto_telefono: string | null;
}

export interface LeadLite {
  id: string; nombre: string | null; telefono: string;
  origen: string | null; marca_interes: string | null;
  etapa_pipeline: string; valor_estimado: number | null;
  icp_score: number | null; icp_nivel: string | null;
  fecha_creacion: string; fecha_ultimo_contacto: string | null;
  usuario_asignado: { id: string; nombre: string } | null;
  empresa_nombre: string | null;
  account_id: string | null;
  contact_id: string | null;
}

export interface CotizacionLite {
  id: string; lead_id: string; folio: string | null;
  total: number; fecha: string; marca: string | null;
  enviada_whatsapp: boolean; enviada_correo: boolean;
  pdf_url: string | null;
}

export interface AccountFull {
  id: string; nombre: string; nombre_comercial: string | null;
  rfc: string | null; industria: string | null; tamano: string | null;
  num_sucursales: number | null; website: string | null;
  telefono: string | null; direccion: string | null;
  ciudad: string | null; estado: string | null; pais: string | null;
  owner_id: string | null; owner_nombre: string | null;
  is_cliente: boolean; notas: string | null;
  cs_account_id: string | null; zoho_account_id: string | null;
  customer_master_id: number | null;
  fecha_creacion: string; fecha_actualizacion: string;
  counts: { leads: number; oportunidades: number; contactos: number; cotizaciones: number };
  leads: LeadLite[];
  oportunidades: OportunidadLite[];
  contactos: ContactoLite[];
  cotizaciones: CotizacionLite[];
  valor_pipe_abierto: number;
  valor_ganado_total: number;
  cs: {
    id: string; client_id: string; mrr: number; arr_proyectado: number;
    sucursales: number; unidades_contratadas: string; tier: string;
    nps: number | null; pulso: string | null;
  } | null;
}
