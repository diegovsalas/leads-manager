"""
Chatbot WhatsApp + Anthropic. Port de vendedores.cloud/chatbot.js.

Recibe webhooks de WhatsApp Cloud API (Meta), genera respuestas con
Claude usando system prompts por unidad, parsea metadata BOT_META para
escalar a cerrador/asesor/frio/descartado, crea Lead automáticamente.

NO confundir con el bot Baileys que vive en /whatsapp-bot/. Este es el
bot oficial Meta Cloud API (multi-cuenta vía ChatbotConfig.unit).

Diferencia clave vs legacy:
- El legacy usa setTimeout para debounce 3s entre mensajes consecutivos.
  Acá lo procesamos inmediatamente. TODO: implementar buffer si causa
  problemas de UX (mensajes partidos generan respuestas por chunk).

Costos: cada generación trackea via api_costs (claude_api / chatbot_message).
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests

from extensions import db
from models import (
    ChatbotConfig, ChatbotConversation, ChatbotMessage,
    Lead, OrigenLead, EtapaPipeline, StateAssignment,
)
from chatbot_prompts import get_system_prompt

log = logging.getLogger("chatbot")

ANTHROPIC_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
_anthropic_client = None


def _get_anthropic():
    """Lazy init del cliente Anthropic — evita crash si la key no está."""
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_KEY:
        try:
            from anthropic import Anthropic
            _anthropic_client = Anthropic(api_key=ANTHROPIC_KEY)
            log.info(f"Anthropic SDK initialized (key {ANTHROPIC_KEY[:12]}...)")
        except Exception as e:
            log.error(f"Anthropic init failed: {e}")
    return _anthropic_client


# ── Webhook verification (GET) ─────────────────────────────────────


def handle_webhook_verification(args: dict) -> tuple[str, int]:
    mode = args.get("hub.mode") or args.get("hub_mode")
    challenge = args.get("hub.challenge") or args.get("hub_challenge")
    token = args.get("hub.verify_token") or args.get("hub_verify_token")
    if mode == "subscribe" and token:
        cfg = ChatbotConfig.query.filter_by(webhook_verify_token=token, active=True).first()
        if cfg:
            log.info(f"Webhook verified for unit: {cfg.unit}")
            return (challenge or ""), 200
    log.warning("Webhook verification failed")
    return ("Forbidden", 403)


# ── Webhook incoming message (POST) ────────────────────────────────


PHONE_UNIT_MAP = {"5531014837": "pestex"}  # display_phone (last 10) → unit


def handle_incoming_message(body: dict) -> dict:
    """Procesa el mensaje entrante. Retorna inmediatamente —
    Meta espera respuesta rápida."""
    try:
        entry = (body.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return {"ok": True, "skipped": "no_messages"}
        meta = value.get("metadata") or {}
        phone_number_id = meta.get("phone_number_id") or ""
        display_phone = meta.get("display_phone_number") or ""
        msg = messages[0]
        from_phone = msg.get("from") or ""
        wa_name = ((value.get("contacts") or [{}])[0].get("profile") or {}).get("name") or ""
        user_message = ((msg.get("text") or {}).get("body") or "").strip()
        if not from_phone or not user_message:
            return {"ok": True, "skipped": "empty"}

        last10 = re.sub(r"\D", "", display_phone)[-10:]
        mapped_unit = PHONE_UNIT_MAP.get(last10)
        if mapped_unit:
            cfg = ChatbotConfig.query.filter_by(unit=mapped_unit, active=True).first()
        else:
            cfg = ChatbotConfig.query.filter_by(phone_number_id=phone_number_id, active=True).first()
        if not cfg:
            log.warning(f"No config for phone {display_phone}")
            return {"ok": True, "skipped": "no_config"}

        log.info(f"[{cfg.unit}] msg from {from_phone}: {user_message[:60]}")
        result = _process_and_respond(from_phone, wa_name, user_message, cfg, phone_number_id)
        return {"ok": True, **result}
    except Exception as e:
        log.exception("handle_incoming_message error")
        return {"ok": False, "error": str(e)}


def _process_and_respond(from_phone: str, wa_name: str, user_message: str,
                          cfg: ChatbotConfig, phone_number_id: str) -> dict:
    unit = cfg.unit

    conv = (
        ChatbotConversation.query
        .filter_by(wa_phone=from_phone, unit=unit, status="activa")
        .first()
    )
    if not conv:
        conv = ChatbotConversation(
            wa_phone=from_phone, wa_name=wa_name, unit=unit,
            status="activa", score=0, lead_data={},
        )
        db.session.add(conv)
        db.session.flush()
        log.info(f"[{unit}] new conv #{conv.id} for {from_phone}")

    db.session.add(ChatbotMessage(conversation_id=conv.id, role="user", content=user_message))
    db.session.commit()

    # Reconstruir historial completo para el contexto
    history = ChatbotMessage.query.filter_by(conversation_id=conv.id).order_by(ChatbotMessage.created_at).all()
    claude_messages = [
        {"role": "user" if m.role == "user" else "assistant", "content": m.content}
        for m in history
    ]

    response_text = _generate_response(claude_messages, unit)
    if not response_text:
        return {"conv_id": conv.id, "skipped": "no_response"}

    clean_message, meta = _parse_bot_meta(response_text)

    db.session.add(ChatbotMessage(conversation_id=conv.id, role="assistant", content=clean_message))

    if meta:
        if "score" in meta:
            try:
                conv.score = int(meta["score"])
            except (ValueError, TypeError):
                pass
        if "lead_data" in meta and isinstance(meta["lead_data"], dict):
            conv.lead_data = meta["lead_data"]

        escalate = meta.get("escalate")
        if escalate == "cerrador":
            _escalate_to_closer(conv, cfg)
        elif escalate == "asesor":
            _escalate_to_advisor(conv, cfg)
        elif escalate == "descartado":
            conv.status = "descartado"
            conv.outcome = "no_califica"
        elif escalate == "frio":
            conv.status = "frio"
            conv.outcome = "seguimiento"
            _create_lead_from_conv(conv, etapa=EtapaPipeline.NUEVO_LEAD, assigned_to=None)
    db.session.commit()

    _send_whatsapp(phone_number_id, from_phone, clean_message, cfg.wa_access_token)
    return {"conv_id": conv.id, "score": conv.score, "outcome": conv.outcome}


# ── Anthropic call ─────────────────────────────────────────────────


def _generate_response(messages: list, unit: str) -> Optional[str]:
    client = _get_anthropic()
    if not client:
        log.error("Anthropic key not configured")
        return None
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            system=get_system_prompt(unit),
            messages=messages,
        )
        in_tok = response.usage.input_tokens if response.usage else 0
        out_tok = response.usage.output_tokens if response.usage else 0
        cost = (in_tok * 0.003 / 1000) + (out_tok * 0.015 / 1000)
        try:
            from api_costs import track_cost
            track_cost(service="claude_api", action="chatbot_message",
                       unit=unit, tokens_input=in_tok,
                       tokens_output=out_tok, cost_usd=cost)
        except Exception:
            pass
        return response.content[0].text if response.content else None
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return None


# ── Parse BOT_META ─────────────────────────────────────────────────


_META_RE = re.compile(r"<!--BOT_META:(.*?)-->", re.DOTALL)


def _parse_bot_meta(text: str) -> tuple[str, Optional[dict]]:
    m = _META_RE.search(text)
    if not m:
        return text, None
    clean = _META_RE.sub("", text).strip()
    try:
        meta = json.loads(m.group(1))
        return clean, meta
    except (json.JSONDecodeError, ValueError):
        return clean, None


# ── Send WhatsApp message via Meta Cloud API ───────────────────────


def _send_whatsapp(phone_number_id: str, to: str, message: str, access_token: Optional[str]) -> None:
    if not (access_token and phone_number_id):
        return
    try:
        resp = requests.post(
            f"https://graph.facebook.com/v21.0/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message}},
            timeout=15,
        )
        data = resp.json() if resp.status_code < 500 else {}
        if data.get("error"):
            log.error(f"WA send error: {data['error'].get('message')}")
        else:
            log.info(f"WA message sent to {to}")
    except Exception as e:
        log.error(f"WA send exception: {e}")


# ── Escalation helpers ─────────────────────────────────────────────


def _create_lead_from_conv(conv: ChatbotConversation, etapa: EtapaPipeline,
                            assigned_to=None) -> Optional[str]:
    """Crea un Lead desde la conversación. Idempotente por phone+unit."""
    ld = conv.lead_data or {}
    last10 = re.sub(r"\D", "", conv.wa_phone)[-10:]
    existing = Lead.query.filter(Lead.telefono.like(f"%{last10}")).first()
    if existing:
        return str(existing.id)
    try:
        lead = Lead(
            telefono=conv.wa_phone,
            nombre=conv.wa_name or f"WhatsApp {conv.wa_phone}",
            empresa_nombre=ld.get("business_type") or "",
            origen=OrigenLead.WHATSAPP_ORGANICO,
            estado_cliente=ld.get("city") or None,
            etapa_pipeline=etapa,
            usuario_asignado_id=assigned_to,
            tipo_cliente=f"Chatbot score: {conv.score} | {ld.get('need','')} | {ld.get('locations',0)} suc.",
        )
        db.session.add(lead)
        db.session.flush()
        return str(lead.id)
    except Exception as e:
        log.warning(f"create lead failed: {e}")
        db.session.rollback()
        return None


def _escalate_to_closer(conv: ChatbotConversation, cfg: ChatbotConfig) -> None:
    closer_id = cfg.closer_user_id
    _create_lead_from_conv(conv, etapa=EtapaPipeline.NEGOCIACION, assigned_to=closer_id)
    conv.status = "escalada_cerrador"
    conv.outcome = "cerrador"
    conv.assigned_to = closer_id
    log.info(f"Escalated to closer: conv #{conv.id} -> {closer_id}")


def _escalate_to_advisor(conv: ChatbotConversation, cfg: ChatbotConfig) -> None:
    ld = conv.lead_data or {}
    city = ld.get("city")
    advisor_id = None
    if city and conv.unit:
        sa = StateAssignment.query.filter_by(state=city, unit=conv.unit).first()
        if sa:
            advisor_id = sa.user_id
    _create_lead_from_conv(conv, etapa=EtapaPipeline.CONTACTO_1, assigned_to=advisor_id)
    conv.status = "escalada_asesor"
    conv.outcome = "asesor"
    conv.assigned_to = advisor_id
    log.info(f"Escalated to advisor: conv #{conv.id} -> {advisor_id}")


# ── Stats ──────────────────────────────────────────────────────────


def get_stats(unit: Optional[str] = None) -> dict:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    week_start = now - timedelta(days=7)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    q = ChatbotConversation.query
    if unit:
        q = q.filter(ChatbotConversation.unit == unit)

    def _count(cond=None):
        sq = q
        if cond is not None:
            sq = sq.filter(cond)
        return sq.count()

    by_unit = (
        db.session.query(ChatbotConversation.unit, db.func.count())
        .group_by(ChatbotConversation.unit).all()
    )
    avg_score = (
        q.filter(ChatbotConversation.score > 0)
        .with_entities(db.func.coalesce(db.func.avg(ChatbotConversation.score), 0))
        .scalar() or 0
    )

    return {
        "today": _count(ChatbotConversation.created_at >= today_start),
        "week": _count(ChatbotConversation.created_at >= week_start),
        "month": _count(ChatbotConversation.created_at >= month_start),
        "total": _count(),
        "escaladaCerrador": _count(ChatbotConversation.outcome == "cerrador"),
        "escaladaAsesor": _count(ChatbotConversation.outcome == "asesor"),
        "frios": _count(ChatbotConversation.outcome == "seguimiento"),
        "descartados": _count(ChatbotConversation.outcome == "no_califica"),
        "avgScore": float(avg_score),
        "byUnit": [{"unit": u or "sin_unit", "c": c} for u, c in by_unit],
        "activas": _count(ChatbotConversation.status == "activa"),
    }
