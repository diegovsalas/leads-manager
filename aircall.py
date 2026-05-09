"""
Aircall integration. Port directo de vendedores.cloud/aircall.js.

Stats de llamadas (totales, respondidas, perdidas, duración, breakdown por
agente). Lee directo de la API de Aircall — no persiste localmente.

Auth: Basic con AIRCALL_API_ID:AIRCALL_API_TOKEN. Rate limit: 60 req/min.
"""
import base64
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("aircall")

AIRCALL_API = "https://api.aircall.io/v1"
API_ID = os.getenv("AIRCALL_API_ID", "")
API_TOKEN = os.getenv("AIRCALL_API_TOKEN", "")


def _auth_header() -> str:
    creds = f"{API_ID}:{API_TOKEN}".encode()
    return "Basic " + base64.b64encode(creds).decode()


def is_configured() -> bool:
    return bool(API_ID and API_TOKEN)


def _api(endpoint: str, params: Optional[dict] = None) -> dict:
    if not is_configured():
        raise RuntimeError("Aircall not configured")
    resp = requests.get(
        AIRCALL_API + endpoint,
        headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
        params=params or {},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Aircall API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _to_unix(value) -> Optional[int]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return None


def get_users() -> dict:
    """GET /users — lista de agentes."""
    return _api("/users")


def get_calls(from_dt=None, to_dt=None, page: int = 1, per_page: int = 50) -> dict:
    """GET /calls — paginated."""
    params = {"per_page": per_page, "page": page, "order": "desc"}
    f = _to_unix(from_dt)
    t = _to_unix(to_dt)
    if f:
        params["from"] = f
    if t:
        params["to"] = t
    return _api("/calls", params)


def get_user_calls(user_id, from_dt=None, to_dt=None, page: int = 1) -> dict:
    params = {"per_page": 50, "page": page, "order": "desc"}
    f = _to_unix(from_dt)
    t = _to_unix(to_dt)
    if f:
        params["from"] = f
    if t:
        params["to"] = t
    return _api(f"/users/{user_id}/calls", params)


def get_all_calls(from_dt=None, to_dt=None) -> list:
    """Trae todas las páginas. Respeta rate limit de Aircall (60 req/min)."""
    all_calls = []
    page = 1
    while True:
        result = get_calls(from_dt, to_dt, page=page, per_page=50)
        calls = result.get("calls") or []
        if not calls:
            break
        all_calls.extend(calls)
        meta = result.get("meta") or {}
        if page >= int(meta.get("total_pages") or 1):
            break
        page += 1
        time.sleep(1.1)  # 60 req/min budget
    return all_calls


def get_call_stats(from_dt=None, to_dt=None) -> dict:
    """Resume llamadas de un rango: total, answered, missed, voicemail, por
    dirección, duración promedio, breakdown por agente."""
    calls = get_all_calls(from_dt, to_dt)
    stats = {
        "total": len(calls), "answered": 0, "missed": 0, "voicemail": 0,
        "inbound": 0, "outbound": 0, "total_duration": 0, "by_user": {},
    }
    for call in calls:
        if call.get("direction") == "inbound":
            stats["inbound"] += 1
        else:
            stats["outbound"] += 1
        if call.get("answered_at"):
            stats["answered"] += 1
        elif call.get("voicemail"):
            stats["voicemail"] += 1
        else:
            stats["missed"] += 1
        stats["total_duration"] += int(call.get("duration") or 0)

        user = call.get("user") or {}
        uid = user.get("id") or 0
        bu = stats["by_user"].setdefault(uid, {
            "name": user.get("name") or "Sin asignar",
            "email": user.get("email") or "",
            "total": 0, "answered": 0, "missed": 0,
            "outbound": 0, "inbound": 0, "total_duration": 0,
        })
        bu["total"] += 1
        if call.get("answered_at"):
            bu["answered"] += 1
        else:
            bu["missed"] += 1
        if call.get("direction") == "outbound":
            bu["outbound"] += 1
        else:
            bu["inbound"] += 1
        bu["total_duration"] += int(call.get("duration") or 0)

    stats["avg_duration"] = round(stats["total_duration"] / stats["answered"]) if stats["answered"] else 0
    stats["answer_rate"] = round((stats["answered"] / stats["total"]) * 100) if stats["total"] else 0
    return stats


def get_connection_info() -> dict:
    return {
        "configured": is_configured(),
        "api_id": (API_ID[:8] + "...") if API_ID else None,
    }
