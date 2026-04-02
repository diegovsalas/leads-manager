-- ============================================================
-- DDL — CRM Avantex · Supabase (PostgreSQL)
-- Ejecutar en orden (respeta dependencias de FK)
-- ============================================================

-- 0. Extensión para generar UUIDs
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Tipos ENUM
CREATE TYPE rol_comercial_enum AS ENUM (
    'Gerente de Ventas',
    'Líder Comercial',
    'Asesor Comercial',
    'SDR'
);

CREATE TYPE origen_lead_enum AS ENUM (
    'Meta Ads',
    'WhatsApp Organico',
    'Web',
    'Prospeccion'
);

CREATE TYPE etapa_pipeline_enum AS ENUM (
    'Nuevo Lead',
    'Calificando',
    'Presentación/Cotización',
    'Seguimiento',
    'Cierre Ganado',
    'Cierre Perdido'
);

CREATE TYPE direccion_mensaje_enum AS ENUM (
    'Entrante',
    'Saliente_Vendedor',
    'Saliente_Bot'
);


-- ============================================================
-- Tabla 1: usuarios (equipo comercial)
-- ============================================================
CREATE TABLE usuarios (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nombre                VARCHAR(150)          NOT NULL,
    telefono_whatsapp     VARCHAR(30),
    rol_comercial         rol_comercial_enum    NOT NULL DEFAULT 'Asesor Comercial',
    especialidad_marca    TEXT[]                NOT NULL DEFAULT '{}',
    ultimo_lead_asignado  TIMESTAMPTZ,
    en_turno              BOOLEAN               NOT NULL DEFAULT TRUE
);

-- Índice para el Round-Robin: vendedores activos ordenados por asignación más antigua
CREATE INDEX idx_usuarios_roundrobin
    ON usuarios (en_turno, ultimo_lead_asignado ASC NULLS FIRST)
    WHERE en_turno = TRUE;


-- ============================================================
-- Tabla 2: leads (oportunidades de venta)
-- ============================================================
CREATE TABLE leads (
    id                    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    telefono              VARCHAR(30)           NOT NULL UNIQUE,
    nombre                VARCHAR(200),
    origen                origen_lead_enum,
    marca_interes         VARCHAR(80),
    etapa_pipeline        etapa_pipeline_enum   NOT NULL DEFAULT 'Nuevo Lead',

    -- Cotizacion
    cantidad_productos    INTEGER,
    precio_unitario       NUMERIC(14, 2),
    valor_estimado        NUMERIC(14, 2),
    motivo_perdida        VARCHAR(300),

    usuario_asignado_id   UUID REFERENCES usuarios(id) ON DELETE SET NULL,

    -- Metadatos Meta Ads
    meta_lead_id          VARCHAR(100) UNIQUE,
    meta_form_id          VARCHAR(100),
    meta_ad_id            VARCHAR(100),
    meta_campaign         VARCHAR(200),

    fecha_creacion        TIMESTAMPTZ           NOT NULL DEFAULT NOW(),
    fecha_actualizacion   TIMESTAMPTZ           NOT NULL DEFAULT NOW()
);

-- Índices para consultas frecuentes del Kanban y filtros
CREATE INDEX idx_leads_etapa       ON leads (etapa_pipeline);
CREATE INDEX idx_leads_vendedor    ON leads (usuario_asignado_id);
CREATE INDEX idx_leads_marca       ON leads (marca_interes);
CREATE INDEX idx_leads_fecha_upd   ON leads (fecha_actualizacion DESC);

-- Trigger para actualizar fecha_actualizacion automáticamente
CREATE OR REPLACE FUNCTION trigger_set_fecha_actualizacion()
RETURNS TRIGGER AS $$
BEGIN
    NEW.fecha_actualizacion = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_leads_updated
    BEFORE UPDATE ON leads
    FOR EACH ROW
    EXECUTE FUNCTION trigger_set_fecha_actualizacion();


-- ============================================================
-- Tabla 3: mensajes_whatsapp (historial de chat de ventas)
-- ============================================================
CREATE TABLE mensajes_whatsapp (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lead_id           UUID              NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direccion         direccion_mensaje_enum NOT NULL,
    contenido         TEXT              NOT NULL,
    meta_message_id   VARCHAR(100)      UNIQUE,
    timestamp         TIMESTAMPTZ       NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_mensajes_lead      ON mensajes_whatsapp (lead_id, timestamp DESC);
CREATE INDEX idx_mensajes_meta_id   ON mensajes_whatsapp (meta_message_id);


-- ============================================================
-- Tabla 4: estado_bot_interno
-- ============================================================
CREATE TABLE estado_bot_interno (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    usuario_id        UUID              NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    lead_contexto_id  UUID              REFERENCES leads(id) ON DELETE SET NULL,
    esperando_input   VARCHAR(50)       NOT NULL DEFAULT 'ninguno'
);

CREATE INDEX idx_bot_usuario ON estado_bot_interno (usuario_id);


-- ============================================================
-- Tabla 5: gastos_publicidad (inversion en ads)
-- ============================================================
CREATE TYPE plataforma_ads_enum AS ENUM (
    'Facebook',
    'Instagram',
    'Google',
    'TikTok',
    'Otro'
);

CREATE TABLE gastos_publicidad (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    plataforma        plataforma_ads_enum   NOT NULL,
    marca             VARCHAR(80),
    campana           VARCHAR(200),
    monto             NUMERIC(14, 2)        NOT NULL,
    fecha             DATE                  NOT NULL,
    notas             VARCHAR(300),
    fecha_registro    TIMESTAMPTZ           NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_gastos_fecha      ON gastos_publicidad (fecha DESC);
CREATE INDEX idx_gastos_plataforma ON gastos_publicidad (plataforma, fecha DESC);
CREATE INDEX idx_gastos_marca      ON gastos_publicidad (marca, fecha DESC);


-- ============================================================
-- Vista: resumen de embudo por mes (para dashboard)
-- ============================================================
CREATE OR REPLACE VIEW v_embudo_mensual AS
WITH leads_mes AS (
    SELECT
        DATE_TRUNC('month', l.fecha_creacion)::DATE  AS mes,
        COUNT(*)                                      AS total_leads,
        COUNT(*) FILTER (WHERE l.etapa_pipeline IN ('Calificando','Presentación/Cotización','Seguimiento','Cierre Ganado'))
                                                      AS calificados,
        COUNT(*) FILTER (WHERE l.etapa_pipeline IN ('Presentación/Cotización','Seguimiento','Cierre Ganado'))
                                                      AS cotizados,
        COUNT(*) FILTER (WHERE l.etapa_pipeline = 'Cierre Ganado')
                                                      AS ganados,
        COUNT(*) FILTER (WHERE l.etapa_pipeline = 'Cierre Perdido')
                                                      AS perdidos,
        COALESCE(SUM(
            CASE WHEN l.etapa_pipeline = 'Cierre Ganado'
                 THEN COALESCE(l.cantidad_productos * l.precio_unitario, l.valor_estimado, 0)
                 ELSE 0
            END
        ), 0)                                         AS revenue_ganado,
        COALESCE(SUM(COALESCE(l.cantidad_productos * l.precio_unitario, l.valor_estimado, 0)), 0)
                                                      AS pipe_total
    FROM leads l
    GROUP BY DATE_TRUNC('month', l.fecha_creacion)
)
SELECT
    lm.*,
    COALESCE((SELECT SUM(g.monto) FROM gastos_publicidad g
              WHERE DATE_TRUNC('month', g.fecha) = lm.mes), 0) AS gasto_ads
FROM leads_mes lm
ORDER BY lm.mes DESC;


-- ============================================================
-- Tabla 6: users_crm (usuarios de la plataforma)
-- ============================================================
CREATE TYPE rol_crm_enum AS ENUM (
    'Super Admin',
    'Admin',
    'Viewer'
);

CREATE TABLE users_crm (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nombre            VARCHAR(150)      NOT NULL,
    correo            VARCHAR(200)      NOT NULL UNIQUE,
    password_hash     VARCHAR(256)      NOT NULL,
    rol               rol_crm_enum      NOT NULL DEFAULT 'Viewer',
    activo            BOOLEAN           NOT NULL DEFAULT TRUE,
    foto_url          VARCHAR(500),
    fecha_creacion    TIMESTAMPTZ       NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_crm_correo ON users_crm (correo);
