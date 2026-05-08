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
