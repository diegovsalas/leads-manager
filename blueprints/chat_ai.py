"""
Asistente IA del CRM — chat con Claude + tool use con RBAC.

Endpoints (/api/chat-ai):
  POST   /message    — envía mensaje, recibe respuesta del asistente
  GET    /history    — últimos N mensajes de la sesión actual
  POST   /clear      — limpia historial de la sesión
  GET    /download/<name>  — sirve archivo CSV/XLS generado por una tool
"""
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, request, jsonify, session, send_file
from sqlalchemy import desc

from extensions import db
from models import UserCRM
import chat_ai_tools as tools

chat_ai_bp = Blueprint("chat_ai", __name__)
log = logging.getLogger("chat_ai")

# Dir para archivos generados (CSV/XLS). En Render usa /tmp (volátil OK).
EXPORTS_DIR = Path(os.getenv("CHAT_EXPORTS_DIR", "/tmp/crm_exports"))
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_TTL_MIN = 60  # archivos viven 60 min

MODEL = os.getenv("CHAT_AI_MODEL", "claude-opus-4-5")
MAX_TOKENS = 1500
MAX_HISTORY = 30  # últimos N mensajes que mandamos a Claude


# ── Helpers ─────────────────────────────────────────────────────────


def _get_session_id() -> str:
    """ID estable de sesión de chat (persiste durante la sesión Flask)."""
    sid = session.get("chat_session_id")
    if not sid:
        sid = secrets.token_urlsafe(16)
        session["chat_session_id"] = sid
    return sid


def _build_ctx() -> dict:
    """Contexto del usuario para las tools."""
    return {
        "user_id":    session.get("user_id"),
        "usuario_id": session.get("usuario_id"),
        "rol":        session.get("user_rol"),
        "nombre":     session.get("user_nombre"),
        "correo":     session.get("user_correo"),
    }


def _system_prompt(ctx: dict) -> str:
    rol = ctx.get("rol", "Vendedor")
    nombre = ctx.get("nombre", "vendedor")
    hoy = datetime.now(timezone.utc).strftime("%A %d de %B de %Y")
    return f"""Eres el asistente IA del CRM de Grupo Avantex. Hablas español de México.

USUARIO: {nombre} · Rol: {rol}
HOY: {hoy}

TU JOB:
1. Resumir pendientes y prioridades del día con claridad accionable
2. Responder preguntas sobre leads, pipeline, vendedores
3. Generar exports CSV/XLS cuando lo pidan
4. Ser conciso. Bullet points cortos. Sin parrafadas largas.

REGLAS:
- Llama tools SOLO cuando la pregunta requiera data fresca de BD. No inventes números.
- Si una tool retorna {{"error": "..."}}, comunícale el problema al usuario con empatía.
- Si {nombre} es Vendedor, NO le des info de otros vendedores. Las tools ya filtran por rol.
- Si {nombre} es Super Admin, puede preguntar por su equipo o cualquier vendedor.
- Al exportar, dale al usuario un link clickeable en markdown: [Descargar archivo](URL)
- Usa montos en MXN con símbolo $ y separadores de miles (ej. $145,300)
- Cuando muestres listas de leads, agrúpalas por etapa y prioriza los estancados (>7 días sin contacto)
- Si te preguntan algo fuera del scope (recetas, política, etc.) redirígelo al CRM con humor leve.

FORMATO:
- Markdown ligero (negritas, listas)
- Emojis solo cuando ayuden (🔴 estancado, ⚠ alerta, ✅ ok)
- Termina cada respuesta con una acción concreta sugerida ("Sugerencia: hoy enfócate en X")
"""


def _save_export_file(headers: list, rows: list, formato: str, user_label: str) -> tuple:
    """Guarda CSV o XLS en EXPORTS_DIR y devuelve (url, filename)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c for c in user_label if c.isalnum() or c in "-_")[:20]
    filename = f"crm_{safe_label}_{ts}.{formato}"
    path = EXPORTS_DIR / filename

    if formato == "xls":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        ws.append(headers)
        for r in rows:
            ws.append(r)
        # autosize cols
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, len(h) + 2)
        wb.save(path)
    else:
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)

    url = f"/api/chat-ai/download/{filename}"
    return url, filename


def _purge_old_exports():
    """Borra archivos > EXPORT_TTL_MIN. Llamado al guardar uno nuevo (no bloquea)."""
    try:
        cutoff = time.time() - (EXPORT_TTL_MIN * 60)
        for p in EXPORTS_DIR.glob("crm_*"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception:
        pass


# ── Historial en BD ────────────────────────────────────────────────


def _load_history_for_claude(user_id: str, session_id: str) -> list:
    """Carga últimos MAX_HISTORY mensajes y los formatea para Claude API."""
    from models import db as _db
    rows = (_db.session.execute(
        _db.text("""
            SELECT role, content FROM chat_messages
            WHERE user_id = :uid AND session_id = :sid
            ORDER BY created_at DESC LIMIT :lim
        """),
        {"uid": user_id, "sid": session_id, "lim": MAX_HISTORY},
    ).fetchall())
    # rows vienen DESC, invertimos para que Claude las vea en orden
    msgs = []
    for r in reversed(rows):
        role = r[0]
        content = r[1]  # ya es JSONB → list de blocks o string
        msgs.append({"role": role, "content": content})
    return msgs


def _save_message(user_id: str, session_id: str, role: str, content):
    """Persiste un mensaje. content puede ser str o list de blocks (tool_use)."""
    db.session.execute(
        db.text("""
            INSERT INTO chat_messages (user_id, session_id, role, content)
            VALUES (:uid, :sid, :role, :content::jsonb)
        """),
        {
            "uid":     user_id,
            "sid":     session_id,
            "role":    role,
            "content": json.dumps(content),
        },
    )
    db.session.commit()


# ── Endpoints ───────────────────────────────────────────────────────


@chat_ai_bp.route("/message", methods=["POST"])
def message():
    """Recibe { "message": "texto del user" }, devuelve { "reply": "...", ... }."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Sin sesión"}), 401

    data = request.get_json() or {}
    user_text = (data.get("message") or "").strip()
    if not user_text:
        return jsonify({"error": "Mensaje vacío"}), 400

    ctx = _build_ctx()
    sid = _get_session_id()

    # Verificar Anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY no configurada en Render"}), 500

    try:
        import anthropic
    except ImportError:
        return jsonify({"error": "anthropic SDK no instalado"}), 500

    client = anthropic.Anthropic(api_key=api_key)

    # Guarda mensaje del usuario
    _save_message(user_id, sid, "user", [{"type": "text", "text": user_text}])

    # Construye conversación para Claude
    messages = _load_history_for_claude(user_id, sid)
    system = _system_prompt(ctx)

    # Loop de tool use (max 5 iteraciones de tools)
    final_text = ""
    iters = 0
    while iters < 5:
        iters += 1
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools.TOOLS_SCHEMA,
                messages=messages,
            )
        except Exception as e:
            log.exception("Anthropic call failed")
            return jsonify({"error": f"Claude falló: {type(e).__name__}: {str(e)[:200]}"}), 502

        # Acumula bloques de la respuesta
        blocks = []
        tool_calls_in_response = []
        for block in resp.content:
            if block.type == "text":
                blocks.append({"type": "text", "text": block.text})
                final_text += block.text
            elif block.type == "tool_use":
                blocks.append({
                    "type":  "tool_use",
                    "id":    block.id,
                    "name":  block.name,
                    "input": block.input,
                })
                tool_calls_in_response.append(block)

        # Persiste el turno del assistant
        _save_message(user_id, sid, "assistant", blocks)
        messages.append({"role": "assistant", "content": blocks})

        if resp.stop_reason == "end_turn" or not tool_calls_in_response:
            break

        # Ejecuta las tools y manda los results al siguiente turno
        tool_results = []
        for tc in tool_calls_in_response:
            result = tools.run_tool(tc.name, tc.input, ctx)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tc.id,
                "content":     json.dumps(result, default=str, ensure_ascii=False),
            })
        _save_message(user_id, sid, "user", tool_results)
        messages.append({"role": "user", "content": tool_results})

    return jsonify({
        "reply":      final_text,
        "session_id": sid,
        "iters":      iters,
    })


@chat_ai_bp.route("/history", methods=["GET"])
def history():
    """Devuelve mensajes de la sesión actual para renderizar el chat al abrirlo."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Sin sesión"}), 401
    sid = _get_session_id()
    rows = (db.session.execute(
        db.text("""
            SELECT role, content, created_at FROM chat_messages
            WHERE user_id = :uid AND session_id = :sid
            ORDER BY created_at ASC LIMIT 100
        """),
        {"uid": user_id, "sid": sid},
    ).fetchall())
    out = []
    for r in rows:
        # Solo regresamos lo visualizable: texto del user y texto del assistant
        content = r[1]
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    out.append({
                        "role":       r[0],
                        "text":       block["text"],
                        "created_at": r[2].isoformat() if r[2] else None,
                    })
                    break
    return jsonify({"messages": out, "session_id": sid})


@chat_ai_bp.route("/clear", methods=["POST"])
def clear():
    """Limpia historial: rota session_id (mantiene histórico viejo, solo no se carga)."""
    if not session.get("user_id"):
        return jsonify({"error": "Sin sesión"}), 401
    session["chat_session_id"] = secrets.token_urlsafe(16)
    return jsonify({"ok": True, "new_session_id": session["chat_session_id"]})


@chat_ai_bp.route("/download/<filename>", methods=["GET"])
def download(filename):
    """Sirve archivos generados por tool_exportar_leads."""
    if not session.get("user_id"):
        return jsonify({"error": "Sin sesión"}), 401
    # Sanitiza filename (no path traversal)
    if "/" in filename or ".." in filename:
        return jsonify({"error": "Filename inválido"}), 400
    path = EXPORTS_DIR / filename
    if not path.exists():
        return jsonify({"error": "Archivo expiró o no existe"}), 404
    return send_file(str(path), as_attachment=True, download_name=filename)
