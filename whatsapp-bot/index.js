/**
 * Microservicio WhatsApp Baileys — CRM Avantex
 *
 * Funcionalidades:
 *  - Múltiples sesiones (1 por Unidad de Negocio)
 *  - QR scan desde el CRM
 *  - Bot de calificación automática
 *  - Webhook a Flask cuando llegan mensajes
 *  - API para enviar mensajes desde Flask
 */

const express = require("express");
const cors = require("cors");
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  makeCacheableSignalKeyStore,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const fs = require("fs");
const path = require("path");

const app = express();
app.use(cors());
app.use(express.json());

const PORT = process.env.PORT || 3001;
const CRM_WEBHOOK_URL =
  process.env.CRM_WEBHOOK_URL ||
  "https://leads-manager-avantex.onrender.com/webhook/baileys";
const BOT_SECRET = process.env.BOT_SECRET || "avantex-bot-2026";

const logger = pino({ level: "info" });

// ══════════════════════════════════════════════
// Sesiones activas: { sessionId: { sock, qr, status, botState } }
// ══════════════════════════════════════════════
const sessions = {};

// ══════════════════════════════════════════════
// Configuración de UN (Unidades de Negocio)
// ══════════════════════════════════════════════
const UNIDADES_NEGOCIO = {
  aromatex: {
    nombre: "Aromatex",
    saludo:
      "¡Hola! Bienvenido a *Aromatex* 🌿\nSomos especialistas en marketing olfativo.\n\n¿Con quién tengo el gusto?",
    servicios: [
      "Aromatización de espacios",
      "Marketing olfativo",
      "Difusores y equipos",
      "Fragancias personalizadas",
    ],
  },
  pestex: {
    nombre: "Pestex",
    saludo:
      "¡Hola! Bienvenido a *Pestex* 🛡️\nExpertos en control de plagas.\n\n¿Con quién tengo el gusto?",
    servicios: [
      "Control de plagas",
      "Fumigación",
      "Desinfección",
      "Mantenimiento preventivo",
    ],
  },
  weldex: {
    nombre: "Weldex",
    saludo:
      "¡Hola! Bienvenido a *Weldex* 🔧\nSoluciones industriales de soldadura.\n\n¿Con quién tengo el gusto?",
    servicios: [
      "Soldadura industrial",
      "Reparaciones",
      "Mantenimiento",
      "Consultoría técnica",
    ],
  },
  nexo: {
    nombre: "Nexo",
    saludo:
      "¡Hola! Bienvenido a *Nexo* 🔗\nConectamos marcas con resultados.\n\n¿Con quién tengo el gusto?",
    servicios: [
      "Marketing digital",
      "Branding",
      "Redes sociales",
      "Campañas publicitarias",
    ],
  },
  aromatex_home: {
    nombre: "Aromatex Home",
    saludo:
      "¡Hola! Bienvenido a *Aromatex Home* 🏠\nAromas para tu hogar.\n\n¿Con quién tengo el gusto?",
    servicios: [
      "Difusores para hogar",
      "Aceites esenciales",
      "Velas aromáticas",
      "Sets de regalo",
    ],
  },
};

// ══════════════════════════════════════════════
// Estado del bot por conversación
// Tracks: { "521234567890": { step, nombre, empresa, sucursales, servicio } }
// ══════════════════════════════════════════════
const botStates = {};

function getBotKey(sessionId, jid) {
  return `${sessionId}:${jid}`;
}

// ══════════════════════════════════════════════
// Conectar sesión Baileys
// ══════════════════════════════════════════════
async function connectSession(sessionId) {
  const authDir = path.join(__dirname, "auth_sessions", sessionId);
  if (!fs.existsSync(authDir)) fs.mkdirSync(authDir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(authDir);

  const sock = makeWASocket({
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, logger),
    },
    logger: pino({ level: "silent" }),
    browser: ["Ubuntu", "Chrome", "20.0.04"],
  });

  sessions[sessionId] = {
    sock,
    qr: null,
    pairingCode: null,
    status: "connecting",
    phoneNumber: null,
  };

  // ── Eventos de conexión ──
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Guardar QR raw — se renderiza en el cliente via CDN
      sessions[sessionId].qr = qr;
      sessions[sessionId].status = "qr_ready";
      logger.info(`[${sessionId}] QR generado — escanea desde el CRM`);

      // Si hay número registrado, generar pairing code también
      const phone = sessions[sessionId].phoneNumber;
      if (phone && !state.creds.registered) {
        try {
          const code = await sock.requestPairingCode(phone);
          sessions[sessionId].pairingCode = code;
          logger.info(`[${sessionId}] Pairing code: ${code}`);
        } catch (e) {
          logger.warn(`[${sessionId}] No se pudo generar pairing code: ${e.message}`);
        }
      }
    }

    if (connection === "open") {
      sessions[sessionId].qr = null;
      sessions[sessionId].pairingCode = null;
      sessions[sessionId].status = "connected";
      logger.info(`[${sessionId}] ✅ Conectado a WhatsApp`);
    }

    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      const shouldReconnect = code !== DisconnectReason.loggedOut;
      logger.warn(
        `[${sessionId}] Desconectado (code=${code}). Reconectar: ${shouldReconnect}`
      );

      if (shouldReconnect) {
        sessions[sessionId].status = "reconnecting";
        setTimeout(() => connectSession(sessionId), 5000);
      } else {
        sessions[sessionId].status = "logged_out";
        fs.rmSync(authDir, { recursive: true, force: true });
      }
    }
  });

  // ── Mensajes entrantes ──
  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;

    for (const msg of messages) {
      if (msg.key.fromMe) continue;
      if (!msg.message) continue;

      const jid = msg.key.remoteJid;
      if (jid === "status@broadcast") continue;

      const telefono = jid.replace("@s.whatsapp.net", "");
      const contenido = extractContent(msg);
      const pushName = msg.pushName || "";

      logger.info(
        `[${sessionId}] Mensaje de ${telefono} (${pushName}): ${contenido}`
      );

      // ── Bot de calificación ──
      const botResponse = await handleBot(sessionId, jid, contenido, pushName);
      if (botResponse) {
        await sock.sendMessage(jid, { text: botResponse.message });
        // Si el bot completó la calificación, enviar webhook
        if (botResponse.completed) {
          await sendWebhook(sessionId, telefono, pushName, contenido, botResponse.leadData);
        }
      } else {
        // Bot inactivo — pasar mensaje directo al CRM
        await sendWebhook(sessionId, telefono, pushName, contenido, null);
      }
    }
  });

  return sessions[sessionId];
}

// ══════════════════════════════════════════════
// Bot de calificación
// ══════════════════════════════════════════════
async function handleBot(sessionId, jid, contenido, pushName) {
  const key = getBotKey(sessionId, jid);
  const un = UNIDADES_NEGOCIO[sessionId] || UNIDADES_NEGOCIO.aromatex;

  // Si el lead ya fue transferido a vendedor, no intervenir
  if (botStates[key] && botStates[key].step === "transferred") {
    return null;
  }

  // Nuevo contacto
  if (!botStates[key]) {
    botStates[key] = { step: "waiting_name", startedAt: Date.now() };
    return { message: un.saludo };
  }

  const state = botStates[key];

  switch (state.step) {
    case "waiting_name":
      state.nombre = contenido.trim();
      state.step = "waiting_empresa";
      return {
        message: `Mucho gusto *${state.nombre}*. ¿De qué empresa nos contacta?`,
      };

    case "waiting_empresa":
      state.empresa = contenido.trim();
      state.step = "waiting_sucursales";
      return {
        message: `¿Cuántas sucursales tienen?`,
      };

    case "waiting_sucursales":
      state.sucursales = contenido.trim();
      state.step = "waiting_servicio";
      const opciones = un.servicios
        .map((s, i) => `${i + 1}. ${s}`)
        .join("\n");
      return {
        message: `¿Qué servicio le interesa?\n\n${opciones}`,
      };

    case "waiting_servicio": {
      const idx = parseInt(contenido.trim()) - 1;
      state.servicio =
        idx >= 0 && idx < un.servicios.length
          ? un.servicios[idx]
          : contenido.trim();
      state.step = "transferred";

      const leadData = {
        nombre: state.nombre,
        empresa: state.empresa,
        sucursales: state.sucursales,
        servicio: state.servicio,
        marca: un.nombre,
        sessionId,
      };

      return {
        message: `Gracias *${state.nombre}*. Tu asesor de *${un.nombre}* te contactará en los próximos minutos por este mismo chat. 🙌`,
        completed: true,
        leadData,
      };
    }

    default:
      return null;
  }
}

// ══════════════════════════════════════════════
// Webhook a Flask
// ══════════════════════════════════════════════
async function sendWebhook(sessionId, telefono, pushName, contenido, leadData) {
  try {
    const payload = {
      secret: BOT_SECRET,
      session_id: sessionId,
      telefono: `+${telefono}`,
      nombre: pushName || (leadData && leadData.nombre) || "",
      contenido,
      lead_data: leadData,
      timestamp: new Date().toISOString(),
    };

    const resp = await fetch(CRM_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      logger.error(`Webhook failed: ${resp.status} ${await resp.text()}`);
    }
  } catch (err) {
    logger.error(`Webhook error: ${err.message}`);
  }
}

// ══════════════════════════════════════════════
// Extraer contenido del mensaje
// ══════════════════════════════════════════════
function extractContent(msg) {
  const m = msg.message;
  if (m.conversation) return m.conversation;
  if (m.extendedTextMessage) return m.extendedTextMessage.text;
  if (m.imageMessage) return m.imageMessage.caption || "[Imagen]";
  if (m.videoMessage) return m.videoMessage.caption || "[Video]";
  if (m.audioMessage) return "[Audio]";
  if (m.documentMessage)
    return m.documentMessage.fileName || "[Documento]";
  if (m.stickerMessage) return "[Sticker]";
  if (m.locationMessage)
    return `[Ubicación: ${m.locationMessage.degreesLatitude}, ${m.locationMessage.degreesLongitude}]`;
  return "[Mensaje no soportado]";
}

// ══════════════════════════════════════════════
// API REST
// ══════════════════════════════════════════════

// Health check
app.get("/health", (req, res) => {
  const status = {};
  for (const [id, s] of Object.entries(sessions)) {
    status[id] = s.status;
  }
  res.json({ ok: true, sessions: status });
});

// Página visual del QR (para escanear desde el celular)
app.get("/scan/:sessionId", (req, res) => {
  const { sessionId } = req.params;
  res.send(`<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp — ${sessionId}</title>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.4/build/qrcode.min.js"><\/script>
<style>*{margin:0;box-sizing:border-box}body{font-family:Inter,system-ui,sans-serif;background:#1e1b3a;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#fff;border-radius:16px;padding:40px;text-align:center;max-width:400px;width:90%;color:#333}
.card h2{font-size:20px;margin-bottom:4px}.card p{font-size:13px;color:#888;margin-bottom:20px}
#qr-box{width:280px;height:280px;margin:0 auto;border-radius:12px;background:#f5f5f5;display:flex;align-items:center;justify-content:center;overflow:hidden}
#qr-box canvas{width:100%!important;height:100%!important;border-radius:12px}
.status{margin-top:16px;font-size:14px;font-weight:600}
.connected{color:#22c55e}.waiting{color:#7c3aed}.error{color:#ef4444}
#pairing{margin-top:12px;font-size:24px;letter-spacing:4px;font-weight:700;color:#7c3aed;display:none}
</style></head><body><div class="card">
<h2>${sessionId.toUpperCase()}</h2>
<p>Escanea el QR desde WhatsApp &gt; Dispositivos vinculados</p>
<div id="qr-box"><span style="color:#aaa">Cargando...</span></div>
<div id="pairing"></div>
<div id="status" class="status waiting">Conectando...</div>
</div>
<script>
var lastQr='';
function poll(){
  fetch('/api/qr/${sessionId}').then(function(r){return r.json()}).then(function(d){
    var box=document.getElementById('qr-box');
    var st=document.getElementById('status');
    var pc=document.getElementById('pairing');
    if(d.status==='connected'){
      box.innerHTML='<div style="font-size:48px">\\u2705</div>';
      st.textContent='Conectado';st.className='status connected';
      pc.style.display='none';return;
    }
    if(d.qr && d.qr!==lastQr){
      lastQr=d.qr;
      box.innerHTML='<canvas id="qr-canvas"></canvas>';
      QRCode.toCanvas(document.getElementById('qr-canvas'),d.qr,{width:280,margin:2},function(err){
        if(err) box.innerHTML='<span style="color:red">Error QR</span>';
      });
      st.textContent='Escanea el QR';st.className='status waiting';
    }
    if(d.pairing_code){pc.textContent=d.pairing_code;pc.style.display='block'}
    setTimeout(poll,3000);
  }).catch(function(){setTimeout(poll,5000)});
}
fetch('/api/session/start',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({session_id:'${sessionId}',secret:'${BOT_SECRET}'})
}).then(function(){setTimeout(poll,2000)});
<\/script></body></html>`);
});

// Obtener QR y/o pairing code para vincular
app.get("/api/qr/:sessionId", (req, res) => {
  const { sessionId } = req.params;
  const session = sessions[sessionId];
  if (!session) return res.status(404).json({ error: "Sesión no encontrada" });
  if (session.status === "connected")
    return res.json({ status: "connected", qr: null, pairing_code: null });
  res.json({
    status: session.status,
    qr: session.qr || null,
    pairing_code: session.pairingCode || null,
  });
});

// Estado de una sesión
app.get("/api/status/:sessionId", (req, res) => {
  const { sessionId } = req.params;
  const session = sessions[sessionId];
  if (!session)
    return res.status(404).json({ error: "Sesión no encontrada", status: "not_started" });
  res.json({ status: session.status });
});

// Iniciar/conectar sesión
// Body: { session_id, secret, phone_number? }
// phone_number es opcional — si se provee, genera pairing code además de QR
app.post("/api/session/start", async (req, res) => {
  const { session_id, secret, phone_number } = req.body;
  if (secret !== BOT_SECRET)
    return res.status(403).json({ error: "No autorizado" });
  if (!session_id)
    return res.status(400).json({ error: "session_id requerido" });

  if (sessions[session_id] && sessions[session_id].status === "connected") {
    return res.json({ status: "already_connected" });
  }

  await connectSession(session_id);

  // Guardar número para pairing code
  if (phone_number && sessions[session_id]) {
    sessions[session_id].phoneNumber = phone_number.replace("+", "");
  }

  res.json({
    status: "connecting",
    message: phone_number
      ? `Espera el pairing code o escanea QR desde /api/qr/${session_id}`
      : `Escanea el QR desde /api/qr/${session_id}`,
  });
});

// Desconectar sesión
app.post("/api/session/disconnect", async (req, res) => {
  const { session_id, secret } = req.body;
  if (secret !== BOT_SECRET)
    return res.status(403).json({ error: "No autorizado" });

  const session = sessions[session_id];
  if (!session) return res.status(404).json({ error: "Sesión no encontrada" });

  await session.sock.logout();
  delete sessions[session_id];
  res.json({ ok: true });
});

// Enviar mensaje (Flask → Baileys → WhatsApp)
app.post("/api/send", async (req, res) => {
  const { session_id, telefono, contenido, secret } = req.body;
  if (secret !== BOT_SECRET)
    return res.status(403).json({ error: "No autorizado" });

  const session = sessions[session_id];
  if (!session || session.status !== "connected") {
    return res.status(400).json({ error: "Sesión no conectada" });
  }

  try {
    const jid = telefono.replace("+", "") + "@s.whatsapp.net";
    const result = await session.sock.sendMessage(jid, { text: contenido });
    res.json({ ok: true, message_id: result.key.id });
  } catch (err) {
    logger.error(`Error enviando: ${err.message}`);
    res.status(500).json({ error: err.message });
  }
});

// Resetear bot state (cuando vendedor toma el chat)
app.post("/api/bot/transfer", (req, res) => {
  const { session_id, telefono, secret } = req.body;
  if (secret !== BOT_SECRET)
    return res.status(403).json({ error: "No autorizado" });

  const jid = telefono.replace("+", "") + "@s.whatsapp.net";
  const key = getBotKey(session_id, jid);
  if (botStates[key]) {
    botStates[key].step = "transferred";
  }
  res.json({ ok: true });
});

// Listar UNs disponibles
app.get("/api/unidades", (req, res) => {
  const data = {};
  for (const [id, un] of Object.entries(UNIDADES_NEGOCIO)) {
    data[id] = { nombre: un.nombre, servicios: un.servicios };
  }
  res.json(data);
});

// ══════════════════════════════════════════════
// Arranque
// ══════════════════════════════════════════════
app.listen(PORT, () => {
  logger.info(`🤖 WhatsApp Bot Avantex corriendo en puerto ${PORT}`);

  // Auto-conectar sesiones que ya tengan auth guardado
  const authDir = path.join(__dirname, "auth_sessions");
  if (fs.existsSync(authDir)) {
    const dirs = fs.readdirSync(authDir);
    for (const d of dirs) {
      const credFile = path.join(authDir, d, "creds.json");
      if (fs.existsSync(credFile)) {
        logger.info(`Auto-conectando sesión: ${d}`);
        connectSession(d);
      }
    }
  }
});
