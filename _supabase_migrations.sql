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
