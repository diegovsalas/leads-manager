-- ═══════════════════════════════════════════════════════════════════
-- LIMPIEZA DE TEST DATA — para arrancar el pipeline limpio
-- ═══════════════════════════════════════════════════════════════════
-- Borra leads y todo el outreach test, PRESERVA:
--   • cs_accounts (tus 25 cuentas clave)
--   • cs_invoices (data cargada por CSV)
--   • customer_master + customer_rfcs (vínculo Savio↔CS)
--   • savio_* (mirror de la API, se repuebla solo)
--   • sdr_dir_master_companies (250 target accounts — son CONFIG, no data test)
--   • sdr_dir_engine_config + sdr_dir_credits_monthly
--   • users_crm + usuarios (login del equipo)
--
-- IMPORTANTE: ejecutá en orden — las FKs requieren borrar children primero.
-- Si querés revisar contenido antes, corré SELECTs primero (comentadas).

BEGIN;

-- 1) Conversaciones / mensajes / chatbot (no afecta leads ni accounts)
TRUNCATE TABLE chatbot_messages, chatbot_conversations CASCADE;
TRUNCATE TABLE mensajes_whatsapp CASCADE;
TRUNCATE TABLE conversaciones CASCADE;
TRUNCATE TABLE estado_bot_interno CASCADE;

-- 2) SDR Prospector y Directivo (resultados/sequences/suggestions/runs)
--    sdr_dir_master_companies se preserva (es config tipo "lista de objetivos")
TRUNCATE TABLE sdr_dir_history CASCADE;
TRUNCATE TABLE sdr_dir_sequences CASCADE;
TRUNCATE TABLE sdr_dir_suggestions CASCADE;
TRUNCATE TABLE sdr_dir_engine_runs CASCADE;
TRUNCATE TABLE sdr_results CASCADE;

-- 3) Sales y clientes post-venta (probablemente vacíos pero por si acaso)
TRUNCATE TABLE clients CASCADE;
TRUNCATE TABLE sales CASCADE;

-- 4) Cotizaciones, gastos, métricas vendedor (test)
TRUNCATE TABLE cotizaciones CASCADE;
TRUNCATE TABLE gastos_publicidad CASCADE;
TRUNCATE TABLE metas_vendedor CASCADE;

-- 5) Leads (lo principal). ActividadLog se preserva por compliance.
TRUNCATE TABLE leads CASCADE;

-- 6) Touchpoints + weekly_kpis (recién creadas, vacías)
TRUNCATE TABLE touchpoints CASCADE;
TRUNCATE TABLE weekly_kpis CASCADE;

-- 7) Resetear secuencias para que IDs vuelvan a empezar en 1 (opcional pero limpio)
ALTER SEQUENCE IF EXISTS leads_id_seq RESTART WITH 1;
ALTER SEQUENCE IF EXISTS sdr_results_id_seq RESTART WITH 1;
ALTER SEQUENCE IF EXISTS sdr_dir_sequences_id_seq RESTART WITH 1;
ALTER SEQUENCE IF EXISTS chatbot_conversations_id_seq RESTART WITH 1;

COMMIT;

-- Si algo sale mal, ROLLBACK; antes del COMMIT.

-- ── Para CONFIRMAR que NO se tocó lo crítico, corré después: ──
-- SELECT COUNT(*) AS cuentas_cs FROM cs_accounts;
-- SELECT COUNT(*) AS facturas_cs FROM cs_invoices;
-- SELECT COUNT(*) AS savio_inv FROM savio_invoices;
-- SELECT COUNT(*) AS master_targets FROM sdr_dir_master_companies;
-- SELECT COUNT(*) AS users FROM users_crm;
