-- Supabase ALTER TABLE statements para cambios que db.create_all() no maneja.
-- Correr en Supabase → SQL Editor antes (o después) del push para que el
-- código nuevo no falle.

-- Round 1: Savio sync (port de vendedores.cloud/savio.js)
-- Tablas nuevas (customer_master, customer_rfcs) las crea db.create_all().
-- Solo necesita ALTER la columna 'sub' en savio_invoices.

ALTER TABLE savio_invoices ADD COLUMN IF NOT EXISTS sub VARCHAR(40);
