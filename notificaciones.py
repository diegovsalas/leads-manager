# notificaciones.py
"""
Notificaciones diarias de tareas pendientes del proyecto.
Se envía a las 9:00 AM CST SOLO si hubo cambios desde el último envío.

Cambios detectados:
- Nuevos avances registrados
- Subtareas completadas o creadas
- Nuevas notas del equipo
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import resend
from models import ProyectoItem, UserCRM
from extensions import db

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
REPLY_TO = "diegovelazquez@grupoavantex.com"
FROM_EMAIL = "CRM Avantex <crm@grupoavantex.com>"

# Equipo del proyecto con sus tareas asignadas
EQUIPO = [
    {
        "nombre": "Diego Velázquez",
        "correo": "diegovelazquez@grupoavantex.com",
        "tareas": [
            ("F14", "ICP Scoring + Nurturing", "12-15 Abr", "Alejandro G. / Diego V."),
            ("F15", "Cotizaciones automáticas (PDF)", "12-15 Abr", "Diego V."),
            ("F17", "Google SSO + Seguridad", "15-17 Abr", "Diego V."),
            ("F10", "WhatsApp Cloud API", "18-21 Abr", "Diego V. / Andrea R."),
            ("F11", "Meta Ads Webhooks", "19-22 Abr", "Diego V. / Andrea R."),
            ("F13", "Bot WhatsApp interno (MVP)", "24-27 Abr", "Diego V."),
        ],
    },
    {
        "nombre": "Alejandro Gil",
        "correo": "alejandrogil@grupoavantex.com",
        "tareas": [
            ("F14", "ICP Scoring + Nurturing", "12-15 Abr", "Alejandro G. / Diego V."),
            ("F16", "Reportes avanzados + Export", "16-19 Abr", "Alejandro G. / Andrea R."),
            ("F18", "Notificaciones push + Email", "22-25 Abr", "Alejandro G. / Andrea R."),
        ],
    },
    {
        "nombre": "Andrea Rodríguez",
        "correo": "andrearodriguez@grupoavantex.com",
        "tareas": [
            ("F16", "Reportes avanzados + Export", "16-19 Abr", "Alejandro G. / Andrea R."),
            ("F10", "WhatsApp Cloud API", "18-21 Abr", "Diego V. / Andrea R."),
            ("F11", "Meta Ads Webhooks", "19-22 Abr", "Diego V. / Andrea R."),
            ("F12", "Chat en vivo (UI bidireccional)", "21-24 Abr", "Andrea R. / Diego V."),
            ("F18", "Notificaciones push + Email", "22-25 Abr", "Alejandro G. / Andrea R."),
        ],
    },
]

CC_ALWAYS = "jessicasantin@grupoavantex.com"

# Bloqueadas
FASES_BLOQUEADAS = {"F10", "F11"}

# Detalle de tareas
TAREA_DETALLE = {
    "F14": "Sistema de calificación automática de leads (score 0-100, niveles A/B/C/D). Alejandro Gil debe validar los pesos y criterios definitivos.",
    "F15": "Generación automática de cotizaciones en PDF. Incluye datos del cliente, productos, precios y condiciones comerciales.",
    "F17": "Login con Google OAuth 2.0 para acceso con cuenta @grupoavantex.com. Reforzar seguridad de sesiones.",
    "F16": "Dashboard con reportes de conversión por vendedor y unidad de negocio. Export a Excel/CSV.",
    "F10": "⚠️ BLOQUEADA — Integración con WhatsApp Business: enviar/recibir mensajes desde el CRM. Requiere acceso a Meta Business.",
    "F11": "⚠️ BLOQUEADA — Recibir leads desde formularios de Meta Ads con asignación Round-Robin. Requiere acceso a Meta Business.",
    "F12": "Chat dentro del CRM para conversar con leads en tiempo real vía WhatsApp.",
    "F18": "Alertas automáticas al vendedor: lead nuevo, lead responde, seguimiento vencido, meta alcanzada.",
    "F13": "Bot para que vendedores gestionen leads desde WhatsApp. Versión mínima viable.",
}


def _hay_cambios_recientes():
    """
    Verifica si hubo cambios en proyecto_items en las últimas 24 horas.
    Retorna (bool, dict con resumen de cambios).
    """
    hace_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    nuevos_avances = ProyectoItem.query.filter(
        ProyectoItem.tipo == "avance",
        ProyectoItem.fecha_creacion >= hace_24h,
    ).all()

    subtareas_actualizadas = ProyectoItem.query.filter(
        ProyectoItem.tipo == "subtarea",
        ProyectoItem.fecha_creacion >= hace_24h,
    ).all()

    nuevas_notas = ProyectoItem.query.filter(
        ProyectoItem.tipo == "nota",
        ProyectoItem.fecha_creacion >= hace_24h,
    ).all()

    total = len(nuevos_avances) + len(subtareas_actualizadas) + len(nuevas_notas)

    return total > 0, {
        "avances": nuevos_avances,
        "subtareas": subtareas_actualizadas,
        "notas": nuevas_notas,
        "total": total,
    }


def _render_cambios_html(cambios):
    """Genera HTML con los cambios recientes."""
    html = ""
    if cambios["avances"]:
        html += '<div style="margin-bottom:12px;"><p style="font-size:13px;font-weight:600;color:#0f7b6c;margin:0 0 6px;">Avances registrados (últimas 24h):</p>'
        for a in cambios["avances"]:
            html += f'<div style="font-size:13px;padding:4px 0;border-bottom:1px solid #ebebea;">• <strong>{a.titulo}</strong>{" — " + a.descripcion if a.descripcion else ""} <span style="color:#787774;">({a.autor})</span></div>'
        html += '</div>'

    if cambios["subtareas"]:
        html += '<div style="margin-bottom:12px;"><p style="font-size:13px;font-weight:600;color:#2eaadc;margin:0 0 6px;">Subtareas nuevas/actualizadas:</p>'
        for s in cambios["subtareas"]:
            check = "✅" if s.completado else "⬜"
            html += f'<div style="font-size:13px;padding:4px 0;border-bottom:1px solid #ebebea;">{check} {s.titulo} <span style="color:#787774;">({s.autor})</span></div>'
        html += '</div>'

    if cambios["notas"]:
        html += '<div style="margin-bottom:12px;"><p style="font-size:13px;font-weight:600;color:#9065b0;margin:0 0 6px;">Notas del equipo:</p>'
        for n in cambios["notas"]:
            html += f'<div style="font-size:13px;padding:4px 0;border-bottom:1px solid #ebebea;">💬 {n.titulo} <span style="color:#787774;">({n.autor})</span></div>'
        html += '</div>'

    return html


def _render_tareas_personales(tareas):
    """Tabla de tareas asignadas al destinatario."""
    rows = ""
    for fase, nombre, fechas, resp in tareas:
        bloqueada = fase in FASES_BLOQUEADAS
        bg = 'background:rgba(235,87,87,.04);' if bloqueada else ''
        color = 'color:#eb5757;' if bloqueada else ''
        badge_style = 'background:#fbe4e4;color:#eb5757;' if bloqueada else 'background:#f1f1ef;color:#787774;'
        badge_text = 'Bloqueada' if bloqueada else 'Pendiente'
        rows += f'''<tr style="{bg}">
            <td style="padding:8px 10px;border-bottom:1px solid #ebebea;font-weight:600;{color}">{fase}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #ebebea;">{nombre}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #ebebea;">{fechas}</td>
            <td style="padding:8px 10px;border-bottom:1px solid #ebebea;"><span style="{badge_style}padding:2px 8px;border-radius:12px;font-size:11px;">{badge_text}</span></td>
        </tr>'''
    return f'''<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px;">
    <thead><tr style="background:#fbfbfa;">
        <th style="text-align:left;padding:8px 10px;border-bottom:2px solid #e3e2e0;font-size:11px;text-transform:uppercase;color:#787774;">Fase</th>
        <th style="text-align:left;padding:8px 10px;border-bottom:2px solid #e3e2e0;font-size:11px;text-transform:uppercase;color:#787774;">Tarea</th>
        <th style="text-align:left;padding:8px 10px;border-bottom:2px solid #e3e2e0;font-size:11px;text-transform:uppercase;color:#787774;">Fechas</th>
        <th style="text-align:left;padding:8px 10px;border-bottom:2px solid #e3e2e0;font-size:11px;text-transform:uppercase;color:#787774;">Estado</th>
    </tr></thead>
    <tbody>{rows}</tbody>
    </table>'''


def _render_detalle_tareas():
    """Cards con descripción de cada tarea."""
    html = '<h2 style="font-size:16px;color:#2eaadc;border-bottom:1px solid #ebebea;padding-bottom:8px;margin-top:28px;">Detalle de cada tarea</h2>'
    for fase, detalle in TAREA_DETALLE.items():
        bloqueada = fase in FASES_BLOQUEADAS
        color = '#eb5757' if bloqueada else '#2eaadc'
        nombre = next((t[1] for eq in EQUIPO for t in eq["tareas"] if t[0] == fase), fase)
        html += f'''<div style="margin-top:8px;padding:12px 16px;border:1px solid #ebebea;border-radius:6px;">
            <p style="font-size:14px;font-weight:600;margin:0 0 4px;color:{color};">{fase} — {nombre}</p>
            <p style="font-size:13px;color:#37352f;margin:0;line-height:1.5;">{detalle}</p>
        </div>'''
    return html


def _render_resumen_equipo():
    """Tabla resumen de todas las tareas del equipo."""
    all_tareas = []
    seen = set()
    for eq in EQUIPO:
        for t in eq["tareas"]:
            if t[0] not in seen:
                seen.add(t[0])
                all_tareas.append(t)

    rows = ""
    for fase, nombre, fechas, resp in all_tareas:
        bloqueada = fase in FASES_BLOQUEADAS
        bg = 'background:rgba(235,87,87,.04);' if bloqueada else ''
        badge_style = 'background:#fbe4e4;color:#eb5757;' if bloqueada else 'background:#f1f1ef;color:#787774;'
        badge_text = 'Bloqueada' if bloqueada else 'Pendiente'
        rows += f'''<tr style="{bg}">
            <td style="padding:6px 8px;border-bottom:1px solid #ebebea;font-weight:600;">{fase}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #ebebea;">{nombre}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #ebebea;">{resp}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #ebebea;">{fechas}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #ebebea;"><span style="{badge_style}padding:2px 6px;border-radius:12px;font-size:10px;">{badge_text}</span></td>
        </tr>'''

    return f'''<h2 style="font-size:16px;color:#37352f;border-bottom:1px solid #ebebea;padding-bottom:8px;margin-top:28px;">Resumen de tareas — Todo el equipo</h2>
    <p style="font-size:13px;color:#787774;margin-bottom:8px;">Si puedes aportar en alguna tarea de otro miembro, coordina directamente.</p>
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
    <thead><tr style="background:#fbfbfa;">
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e3e2e0;color:#787774;">Fase</th>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e3e2e0;color:#787774;">Tarea</th>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e3e2e0;color:#787774;">Responsable</th>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e3e2e0;color:#787774;">Fechas</th>
        <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e3e2e0;color:#787774;">Estado</th>
    </tr></thead>
    <tbody>{rows}</tbody>
    </table>'''


def enviar_notificaciones_diarias():
    """
    Envía correo de tareas pendientes a cada miembro del equipo.
    SOLO se envía si hubo cambios en las últimas 24 horas.
    """
    if not RESEND_API_KEY:
        print("[Notificaciones] RESEND_API_KEY no configurada, saltando", file=sys.stderr)
        return

    resend.api_key = RESEND_API_KEY

    hay_cambios, cambios = _hay_cambios_recientes()
    if not hay_cambios:
        print("[Notificaciones] Sin cambios en las últimas 24h, no se envía correo", file=sys.stderr)
        return

    hoy = datetime.now(timezone.utc).strftime("%d de %B %Y").replace(
        "January", "Enero").replace("February", "Febrero").replace("March", "Marzo"
    ).replace("April", "Abril").replace("May", "Mayo")

    cambios_html = _render_cambios_html(cambios)
    detalle_html = _render_detalle_tareas()
    resumen_html = _render_resumen_equipo()

    enviados = 0
    for miembro in EQUIPO:
        tareas_html = _render_tareas_personales(miembro["tareas"])

        html = f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#37352f;">

        <div style="border-bottom:2px solid #2eaadc;padding-bottom:16px;margin-bottom:24px;">
          <h1 style="font-size:22px;font-weight:700;margin:0;">Leads Manager — Grupo Avantex</h1>
          <p style="font-size:14px;color:#787774;margin:4px 0 0;">Seguimiento de proyecto · {hoy}</p>
        </div>

        <p style="font-size:14px;line-height:1.6;">Hola {miembro['nombre'].split()[0]},</p>
        <p style="font-size:14px;line-height:1.6;">Hubo <strong>{cambios['total']} actualización(es)</strong> en el proyecto en las últimas 24 horas. Aquí tienes el resumen y tus tareas pendientes. <strong style="color:#eb5757;">Deadline: 30 de Abril.</strong></p>

        <h2 style="font-size:16px;color:#0f7b6c;border-bottom:1px solid #ebebea;padding-bottom:8px;margin-top:24px;">Actividad reciente</h2>
        {cambios_html}

        <h2 style="font-size:16px;color:#2eaadc;border-bottom:1px solid #ebebea;padding-bottom:8px;margin-top:24px;">Tus tareas asignadas</h2>
        {tareas_html}

        {detalle_html}
        {resumen_html}

        <p style="font-size:14px;line-height:1.6;margin-top:20px;">Cualquier bloqueador o retraso repórtalo en <strong>Notas del equipo</strong> en el CRM:<br>
        <a href="https://leads-manager-avantex.onrender.com" style="color:#2eaadc;">leads-manager-avantex.onrender.com</a></p>

        <p style="font-size:14px;line-height:1.6;margin-top:16px;"><strong>Project Management IA — Grupo Avantex</strong></p>
        </div>"""

        try:
            resend.Emails.send({
                "from": FROM_EMAIL,
                "to": [miembro["correo"]],
                "cc": [CC_ALWAYS],
                "reply_to": REPLY_TO,
                "subject": f"[Leads Manager] Tareas pendientes — {miembro['nombre']} ({hoy})",
                "html": html,
            })
            enviados += 1
            print(f"[Notificaciones] Enviado a {miembro['nombre']}", file=sys.stderr)
        except Exception as e:
            print(f"[Notificaciones] Error enviando a {miembro['nombre']}: {e}", file=sys.stderr)

    print(f"[Notificaciones] {enviados}/{len(EQUIPO)} correos enviados", file=sys.stderr)
