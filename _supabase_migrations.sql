-- Supabase ALTER TABLE statements para cambios que db.create_all() no maneja.
-- Correr en Supabase → SQL Editor antes (o después) del push para que el
-- código nuevo no falle.

-- Round 1: Savio sync (port de vendedores.cloud/savio.js)
-- Tablas nuevas (customer_master, customer_rfcs) las crea db.create_all().
-- Solo necesita ALTER la columna 'sub' en savio_invoices.

ALTER TABLE savio_invoices ADD COLUMN IF NOT EXISTS sub VARCHAR(40);

-- Round Savio→CS: idempotent key para sync de invoices desde Savio
ALTER TABLE cs_invoices ADD COLUMN IF NOT EXISTS savio_invoice_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_cs_invoices_savio_invoice_id ON cs_invoices(savio_invoice_id);

-- CSV de cobros CS: llave idempotente para evitar duplicados al reimportar.
ALTER TABLE cs_invoices ADD COLUMN IF NOT EXISTS cs_import_key VARCHAR(80);
UPDATE cs_invoices
SET cs_import_key = CASE
    WHEN NULLIF(BTRIM(folio), '') IS NOT NULL THEN
        'csv:' || md5(concat_ws('|',
            'folio',
            account_id::text,
            lower(BTRIM(coalesce(serie, ''))),
            lower(BTRIM(folio))
        ))
    ELSE
        'csv:' || md5(concat_ws('|',
            'fallback',
            account_id::text,
            lower(BTRIM(coalesce(concepto, ''))),
            coalesce(fecha_cobro::text, ''),
            trim(to_char(coalesce(total, 0), '999999999999990.00'))
        ))
END
WHERE cs_import_key IS NULL
  AND savio_invoice_id IS NULL;
DELETE FROM cs_invoices a
USING cs_invoices b
WHERE a.cs_import_key IS NOT NULL
  AND a.cs_import_key = b.cs_import_key
  AND a.id > b.id;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_invoices_import_key
    ON cs_invoices(cs_import_key)
    WHERE cs_import_key IS NOT NULL;

-- Drop FKs en tablas mirror de Savio: solo importamos los ultimos 90d, los
-- payments pueden referenciar invoices fuera de esa ventana → FK rompe.
-- Las tablas savio_* son espejo de Savio; no garantizamos integridad local.
ALTER TABLE savio_payments      DROP CONSTRAINT IF EXISTS savio_payments_invoice_id_fkey;
ALTER TABLE savio_payments      DROP CONSTRAINT IF EXISTS savio_payments_customer_id_fkey;
ALTER TABLE savio_invoices      DROP CONSTRAINT IF EXISTS savio_invoices_customer_id_fkey;
ALTER TABLE savio_subscriptions DROP CONSTRAINT IF EXISTS savio_subscriptions_customer_id_fkey;

-- Fase 3 (Account + Contact): agregar columnas account_id + contact_id a
-- leads + oportunidades. Sin FK constraint estricto en DB (mismo patrón
-- que Savio: soft links). Las tablas accounts y contacts las crea
-- db.create_all() automáticamente al deploy.
ALTER TABLE leads          ADD COLUMN IF NOT EXISTS account_id UUID;
ALTER TABLE leads          ADD COLUMN IF NOT EXISTS contact_id UUID;
ALTER TABLE oportunidades  ADD COLUMN IF NOT EXISTS account_id UUID;
ALTER TABLE oportunidades  ADD COLUMN IF NOT EXISTS contact_id UUID;
CREATE INDEX IF NOT EXISTS idx_leads_account_id          ON leads(account_id);
CREATE INDEX IF NOT EXISTS idx_leads_contact_id          ON leads(contact_id);
CREATE INDEX IF NOT EXISTS idx_oportunidades_account_id  ON oportunidades(account_id);
CREATE INDEX IF NOT EXISTS idx_oportunidades_contact_id  ON oportunidades(contact_id);

-- Zoho Analytics ETL: clave única para upsert idempotente de citas.
-- El script zoho_appointments_etl.py usa esta columna como onConflict.
ALTER TABLE cs_appointments
    ADD COLUMN IF NOT EXISTS zoho_appointment_id VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_appointments_zoho_id
    ON cs_appointments(zoho_appointment_id)
    WHERE zoho_appointment_id IS NOT NULL;

-- customer_rfcs: el RFC NO debe ser único — múltiples clientes Savio
-- comparten el RFC genérico "XAXX010101000" (público en general MX).
-- Lo único que sí debe ser único es savio_customer_id.
-- Antes de cambiar el constraint, limpiamos duplicados existentes por savio_customer_id.
DELETE FROM customer_rfcs a USING customer_rfcs b
    WHERE a.id > b.id AND a.savio_customer_id = b.savio_customer_id;
ALTER TABLE customer_rfcs DROP CONSTRAINT IF EXISTS customer_rfcs_rfc_key;
DROP INDEX IF EXISTS ix_customer_rfcs_rfc;  -- era unique, lo reemplazamos
CREATE INDEX IF NOT EXISTS ix_customer_rfcs_rfc ON customer_rfcs(rfc);
ALTER TABLE customer_rfcs
    ADD CONSTRAINT customer_rfcs_savio_customer_id_unique UNIQUE (savio_customer_id);

-- Oportunidades: split del campo 'contacto' (texto libre) en 3 sub-campos
-- (persona, teléfono, email). 'contacto' queda como nombre de la persona.
ALTER TABLE cs_opportunities
    ADD COLUMN IF NOT EXISTS contacto_telefono VARCHAR(40) DEFAULT '',
    ADD COLUMN IF NOT EXISTS contacto_email    VARCHAR(200) DEFAULT '';

-- Leads: campo libre "información importante" / notas del lead.
-- Antes vivía dentro del <details> oculto del modal y se guardaba en tipo_cliente
-- (hack). Ahora tiene columna propia y campo visible en el formulario.
ALTER TABLE leads ADD COLUMN IF NOT EXISTS notas TEXT;

-- Round CRM/PIPE integridad: una oportunidad ganada debe reflejarse como una
-- venta única, y Account/Contact dejan de ser soft links huérfanos.
ALTER TABLE sales ADD COLUMN IF NOT EXISTS opportunity_id UUID;
CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_opportunity_id
    ON sales(opportunity_id)
    WHERE opportunity_id IS NOT NULL;

UPDATE sales s
SET opportunity_id = NULL
WHERE opportunity_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM oportunidades o WHERE o.id = s.opportunity_id);

ALTER TABLE sales DROP CONSTRAINT IF EXISTS sales_opportunity_id_fkey;
ALTER TABLE sales
    ADD CONSTRAINT sales_opportunity_id_fkey
    FOREIGN KEY (opportunity_id)
    REFERENCES oportunidades(id)
    ON DELETE SET NULL;

UPDATE leads l
SET account_id = NULL
WHERE account_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id = l.account_id);
UPDATE leads l
SET contact_id = NULL
WHERE contact_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = l.contact_id);
UPDATE oportunidades o
SET account_id = NULL
WHERE account_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.id = o.account_id);
UPDATE oportunidades o
SET contact_id = NULL
WHERE contact_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = o.contact_id);

ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_account_id_fkey;
ALTER TABLE leads
    ADD CONSTRAINT leads_account_id_fkey
    FOREIGN KEY (account_id)
    REFERENCES accounts(id)
    ON DELETE SET NULL;
ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_contact_id_fkey;
ALTER TABLE leads
    ADD CONSTRAINT leads_contact_id_fkey
    FOREIGN KEY (contact_id)
    REFERENCES contacts(id)
    ON DELETE SET NULL;
ALTER TABLE oportunidades DROP CONSTRAINT IF EXISTS oportunidades_account_id_fkey;
ALTER TABLE oportunidades
    ADD CONSTRAINT oportunidades_account_id_fkey
    FOREIGN KEY (account_id)
    REFERENCES accounts(id)
    ON DELETE SET NULL;
ALTER TABLE oportunidades DROP CONSTRAINT IF EXISTS oportunidades_contact_id_fkey;
ALTER TABLE oportunidades
    ADD CONSTRAINT oportunidades_contact_id_fkey
    FOREIGN KEY (contact_id)
    REFERENCES contacts(id)
    ON DELETE SET NULL;

-- FEAT-2026-07-07: dirección de correos (IN=recibido / OUT=enviado)
ALTER TABLE sales_emails ADD COLUMN IF NOT EXISTS direccion VARCHAR(4) DEFAULT 'OUT';
CREATE INDEX IF NOT EXISTS ix_sales_emails_direccion ON sales_emails (direccion);
-- Backfill: todos los correos previos son enviados (OUT)
UPDATE sales_emails SET direccion = 'OUT' WHERE direccion IS NULL;

-- FEAT-2026-07-07: timestamp de backfill inicial de correos recibidos por vendedor
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS gmail_backfilled_in_at TIMESTAMPTZ;

-- FEAT-2026-07-08: privacidad de correos — restringe visibilidad por admin
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS correos_visibles_para_user_id UUID;
-- Seed: Andrés Garza Martínez solo visible para Diego Velázquez
UPDATE usuarios u
SET correos_visibles_para_user_id = (
    SELECT id FROM users_crm
    WHERE nombre ILIKE '%diego velazquez%'
    LIMIT 1
)
WHERE u.nombre ILIKE '%andres%garza%martinez%'
  AND correos_visibles_para_user_id IS NULL;

-- ══════════════════════════════════════════════════════════════
-- FEAT-2026-08-14: Client View — vista individual de cliente con
-- banner de highlights fijo y 4 pestañas (Overview / Proyectos /
-- Historial / Documentación).
-- ══════════════════════════════════════════════════════════════

-- Renovación de contrato. No existe fuente automática: savio_subscriptions
-- .contract_end_date solo está poblado en subs 'ended' (1322/1322) y en 6
-- de 1736 activas, o sea registra cuándo TERMINÓ un contrato, no cuándo
-- renueva. Se captura manualmente por el KAM.
ALTER TABLE cs_accounts ADD COLUMN IF NOT EXISTS fecha_renovacion DATE;
ALTER TABLE cs_accounts ADD COLUMN IF NOT EXISTS valor_contrato NUMERIC(14,2);
CREATE INDEX IF NOT EXISTS ix_cs_accounts_fecha_renovacion
    ON cs_accounts (fecha_renovacion) WHERE fecha_renovacion IS NOT NULL;

-- OKRs / objetivos del cliente (tab Overview)
CREATE TABLE IF NOT EXISTS cs_objetivos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES cs_accounts(id) ON DELETE CASCADE,
    titulo          VARCHAR(300) NOT NULL,
    descripcion     TEXT DEFAULT '',
    metrica         VARCHAR(120) DEFAULT '',
    valor_objetivo  NUMERIC(14,2),
    valor_actual    NUMERIC(14,2),
    unidad          VARCHAR(30) DEFAULT '',
    fecha_objetivo  DATE,
    estatus         VARCHAR(30) DEFAULT 'En progreso',
    orden           INTEGER DEFAULT 0,
    created_by      VARCHAR(200) DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_cs_objetivos_account ON cs_objetivos (account_id);

-- Documentación y accesos (tab Documentación)
CREATE TABLE IF NOT EXISTS cs_documentos (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   UUID NOT NULL REFERENCES cs_accounts(id) ON DELETE CASCADE,
    titulo       VARCHAR(300) NOT NULL,
    tipo         VARCHAR(40) DEFAULT 'Otro',
    url          TEXT NOT NULL,
    descripcion  TEXT DEFAULT '',
    orden        INTEGER DEFAULT 0,
    created_by   VARCHAR(200) DEFAULT '',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_cs_documentos_account ON cs_documentos (account_id);

-- FIX-2026-08-14: los OKR de reducción (ej. "bajar incidencias de 8 a 4")
-- calculaban el avance al revés porque progreso_pct asumía que más es mejor.
ALTER TABLE cs_objetivos ADD COLUMN IF NOT EXISTS direccion VARCHAR(10) DEFAULT 'subir';
ALTER TABLE cs_objetivos ADD COLUMN IF NOT EXISTS valor_inicial NUMERIC(14,2);

-- ══════════════════════════════════════════════════════════════
-- FEAT-2026-08-21: Comisiones multi-UN repartidas por etapa.
-- Implementa el "Tabulador de comisiones multiUN": la bolsa que
-- cada UN ya paga hoy se reparte entre 4 etapas, y cada quien
-- cobra las que hizo. NO incrementa el costo de comisión.
-- ══════════════════════════════════════════════════════════════

-- Tasa por UN. Un tramo único (monto_hasta NULL) = tasa fija.
-- Pestex es escalonado, por eso se modela con tramos y no con
-- una columna suelta.
CREATE TABLE IF NOT EXISTS comision_tasas (
    id           SERIAL PRIMARY KEY,
    unidad       VARCHAR(40)   NOT NULL,
    monto_desde  NUMERIC(14,2) NOT NULL DEFAULT 0,
    monto_hasta  NUMERIC(14,2),              -- NULL = sin tope
    tasa         NUMERIC(6,4)  NOT NULL,     -- 0.4000 = 40%
    modalidad    VARCHAR(60)   DEFAULT 'Pago único, mes 1',
    regla_origen TEXT          DEFAULT '',
    nota         TEXT          DEFAULT '',   -- candados y salvedades
    activo       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ   DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_comision_tasas_un ON comision_tasas (unidad, monto_desde);

-- Pesos de las 4 etapas. Deben sumar 1.0000.
CREATE TABLE IF NOT EXISTS comision_etapas (
    id          SERIAL PRIMARY KEY,
    clave       VARCHAR(40)  NOT NULL UNIQUE,
    nombre      VARCHAR(80)  NOT NULL,
    peso        NUMERIC(6,4) NOT NULL,
    descripcion TEXT DEFAULT '',
    orden       INTEGER NOT NULL DEFAULT 0
);

-- Castigo por descuento en el cierre.
-- OJO: descuento_desde es el PISO EXCLUSIVO del tramo. El precio de
-- lista (descuento = 0) NO entra aquí: se resuelve antes, con factor
-- 1.0. En el Sheet el tramo de 85% aparece con piso "0.0%", lo que
-- haría que una venta sin descuento cobrara 85%.
CREATE TABLE IF NOT EXISTS comision_descuentos (
    id              SERIAL PRIMARY KEY,
    descuento_desde NUMERIC(6,4) NOT NULL,
    factor          NUMERIC(6,4) NOT NULL,
    etiqueta        VARCHAR(120) DEFAULT '',
    requiere_autorizacion BOOLEAN NOT NULL DEFAULT FALSE
);

-- Quién hizo cada etapa de un lead. Se propone automáticamente al
-- mover el lead en el pipeline y el gerente puede corregirlo.
CREATE TABLE IF NOT EXISTS lead_atribuciones (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id      UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    etapa        VARCHAR(40) NOT NULL,
    usuario_id   UUID REFERENCES usuarios(id),
    es_automatica BOOLEAN NOT NULL DEFAULT TRUE,
    definida_por VARCHAR(200) DEFAULT '',
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (lead_id, etapa)
);
CREATE INDEX IF NOT EXISTS ix_lead_atribuciones_lead ON lead_atribuciones (lead_id);

-- Reparto congelado al cerrar la venta. Se guarda el resultado, no
-- se recalcula: si mañana cambian los pesos, lo ya pagado no se mueve.
CREATE TABLE IF NOT EXISTS sale_participaciones (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sale_id     UUID NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    usuario_id  UUID REFERENCES usuarios(id),
    etapas      TEXT NOT NULL DEFAULT '',    -- 'Prospectar, Cita'
    porcentaje  NUMERIC(6,4) NOT NULL,       -- 0.3000 = 30%
    monto       NUMERIC(14,2) NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_sale_participaciones_sale ON sale_participaciones (sale_id);
CREATE INDEX IF NOT EXISTS ix_sale_participaciones_user ON sale_participaciones (usuario_id);
