"""
Monitor de correos salientes de vendedores (Gmail Workspace).

Lee correos enviados (in:sent) por cada vendedor con gmail_address registrado
en `usuarios`, filtrando para que el destinatario NO sea @grupoavantex.com
(solo correos a clientes/prospectos externos), y los persiste en sales_emails.

Auth: Service Account con domain-wide delegation. La cuenta de servicio
impersona cada vendedor para acceder a su Gmail (read-only).

Config:
  GMAIL_SERVICE_ACCOUNT_JSON  — string con el JSON del service account
  GMAIL_INTERNAL_DOMAIN       — dominio interno a excluir (default 'grupoavantex.com')
  GMAIL_POLL_LOOKBACK_MIN     — cuántos minutos atrás revisar en cada poll (default 15)
  GMAIL_RETENTION_DAYS        — días a conservar en BD (default 365)
"""
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from extensions import db
from models import Usuario, SalesEmail

log = logging.getLogger("gmail_monitor")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
INTERNAL_DOMAIN = os.getenv("GMAIL_INTERNAL_DOMAIN", "grupoavantex.com").lower()
LOOKBACK_MIN = int(os.getenv("GMAIL_POLL_LOOKBACK_MIN", "15"))
RETENTION_DAYS = int(os.getenv("GMAIL_RETENTION_DAYS", "365"))


def _load_credentials_json() -> Optional[dict]:
    raw = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"GMAIL_SERVICE_ACCOUNT_JSON inválido: {e}")
        return None


def is_configured() -> bool:
    return _load_credentials_json() is not None


def _build_service(impersonate_email: str):
    """Construye cliente Gmail API impersonando un usuario del dominio."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_info = _load_credentials_json()
    if not creds_info:
        raise RuntimeError("GMAIL_SERVICE_ACCOUNT_JSON no configurado")
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES, subject=impersonate_email,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers: list, name: str) -> Optional[str]:
    """Extrae el valor de un header por nombre case-insensitive."""
    name_lower = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == name_lower:
            return h.get("value")
    return None


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _extract_emails(header_value: Optional[str]) -> list:
    """De un header tipo To/Cc 'Foo <a@b.com>, c@d.com' saca lista de emails."""
    if not header_value:
        return []
    return [e.lower() for e in _EMAIL_RE.findall(header_value)]


def _all_external(emails: list) -> bool:
    """True si la lista tiene al menos un email Y ninguno es del dominio interno."""
    if not emails:
        return False
    return all(not e.endswith("@" + INTERNAL_DOMAIN) for e in emails)


def _parse_gmail_message(msg: dict) -> Optional[dict]:
    """Convierte un message resource de Gmail API a dict listo para SalesEmail."""
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    to_raw = _header(headers, "To")
    cc_raw = _header(headers, "Cc")
    from_raw = _header(headers, "From")
    subject = _header(headers, "Subject") or ""
    date_raw = _header(headers, "Date")

    to_list = _extract_emails(to_raw)
    cc_list = _extract_emails(cc_raw)
    from_list = _extract_emails(from_raw)
    from_email = from_list[0] if from_list else ""

    # FILTRO clave: si CUALQUIER destinatario es interno, descartar el correo
    # (solo capturamos correos genuinamente externos a clientes/prospectos)
    if not to_list and not cc_list:
        return None
    all_dests = to_list + cc_list
    if any(e.endswith("@" + INTERNAL_DOMAIN) for e in all_dests):
        return None
    if not _all_external(all_dests):
        return None

    # internalDate es ms desde epoch (más confiable que Date header)
    internal_ms = msg.get("internalDate")
    sent_at = None
    if internal_ms:
        try:
            sent_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    if not sent_at:
        sent_at = datetime.now(timezone.utc)

    # has_attachment: si alguna parte tiene filename no vacío
    has_attachment = _has_attachment(payload)

    # snippet: Gmail lo provee directo (típicamente ~100-200 chars)
    snippet = (msg.get("snippet") or "")[:200]

    return {
        "gmail_message_id": msg.get("id"),
        "gmail_thread_id":  msg.get("threadId"),
        "sent_at":          sent_at,
        "from_email":       from_email,
        "to_emails":        to_list,
        "cc_emails":        cc_list,
        "subject":          subject[:500] if subject else None,
        "snippet":          snippet,
        "has_attachment":   has_attachment,
    }


def _has_attachment(payload: dict) -> bool:
    if not payload:
        return False
    if payload.get("filename"):
        return True
    for part in (payload.get("parts") or []):
        if _has_attachment(part):
            return True
    return False


def _query_for_lookback(minutes: int) -> str:
    """Gmail query: enviados en los últimos N minutos, excluyendo destinatarios
    internos. `newer_than` con unidad mínima h; para minutos usamos `after:`."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp())
    # Filtro a nivel de query: -to:@dominio (puede haber falsos negativos si hay
    # múltiples destinatarios mezclados; el parser hace doble check después)
    return f"in:sent after:{after_ts} -to:@{INTERNAL_DOMAIN}"


def poll_vendor(vendedor: Usuario, lookback_min: int = LOOKBACK_MIN) -> dict:
    """Pull correos salientes de un vendedor. Retorna stats."""
    stats = {"vendedor": vendedor.nombre, "fetched": 0, "saved": 0, "skipped_internal": 0, "errors": 0}
    if not vendedor.gmail_address:
        return stats

    try:
        svc = _build_service(vendedor.gmail_address)
    except Exception as e:
        log.exception(f"build_service falló para {vendedor.gmail_address}: {e}")
        stats["errors"] = 1
        return stats

    query = _query_for_lookback(lookback_min)
    try:
        resp = svc.users().messages().list(userId="me", q=query, maxResults=100).execute()
    except Exception as e:
        log.exception(f"list falló para {vendedor.gmail_address}: {e}")
        stats["errors"] = 1
        return stats

    messages = resp.get("messages") or []
    stats["fetched"] = len(messages)

    for m in messages:
        mid = m.get("id")
        if not mid:
            continue
        # Skip si ya está en BD
        if db.session.query(SalesEmail.id).filter_by(gmail_message_id=mid).first():
            continue

        try:
            # metadata solo: ahorra ancho de banda y respeta privacidad
            msg = svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["To", "Cc", "From", "Subject", "Date"],
            ).execute()
        except Exception as e:
            log.warning(f"get message {mid} falló: {e}")
            stats["errors"] += 1
            continue

        parsed = _parse_gmail_message(msg)
        if not parsed:
            stats["skipped_internal"] += 1
            continue

        email = SalesEmail(
            vendedor_id=vendedor.id,
            **parsed,
        )
        db.session.add(email)
        try:
            db.session.flush()
            stats["saved"] += 1
        except Exception as e:
            db.session.rollback()
            log.warning(f"insert msg {mid} falló (probable dup): {e}")
            stats["errors"] += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.exception(f"commit final falló: {e}")
        stats["errors"] += 1

    return stats


def poll_all(lookback_min: int = LOOKBACK_MIN) -> dict:
    """Itera todos los vendedores con gmail_address y los polea."""
    if not is_configured():
        return {"error": "GMAIL_SERVICE_ACCOUNT_JSON no configurado"}

    vendedores = Usuario.query.filter(
        Usuario.gmail_address.isnot(None),
        Usuario.gmail_address != "",
    ).all()

    results = []
    for v in vendedores:
        results.append(poll_vendor(v, lookback_min=lookback_min))
    return {
        "vendedores": len(vendedores),
        "total_saved": sum(r["saved"] for r in results),
        "total_fetched": sum(r["fetched"] for r in results),
        "detalle": results,
    }


def purge_old(days: int = RETENTION_DAYS) -> dict:
    """Borra registros con sent_at más viejos que `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = SalesEmail.query.filter(SalesEmail.sent_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return {"deleted": deleted, "cutoff": cutoff.isoformat()}
