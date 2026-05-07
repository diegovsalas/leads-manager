"""
Cliente HTTP para la API de Savio.

Solo lectura. Devuelve dicts crudos — el mapeo a modelos vive en savio_sync.py.

Auth: header `Authorization: <API_KEY>` (token plano, sin "Bearer").

Paginación (cada endpoint la maneja distinto, port directo de savio.js legacy):
  /customer            → lista plana, sin paginación
  /subscription/search → page-based: response.next_page, items en `subscriptions`
  /invoice             → cursor-based: response.nextCursor, items en `invoices`
  /payment             → cursor-based: response.nextCursor, items en `payments`
"""

import os
import sys
from typing import Iterator, Optional

import requests

SAVIO_BASE_URL = os.getenv("SAVIO_BASE_URL", "https://api.savio.mx/api/v1")

DEFAULT_LIMIT = 100
DEFAULT_TIMEOUT = 30
MAX_PAGES = 500


class SavioError(Exception):
    """Error genérico hablando con la API de Savio."""


def _api_key() -> str:
    key = os.getenv("SAVIO_API_KEY", "")
    if not key:
        raise SavioError("SAVIO_API_KEY no configurada en .env")
    return key


def _headers() -> dict:
    return {"Authorization": _api_key(), "Accept": "application/json"}


def _get(path: str, params: Optional[dict] = None) -> dict:
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    resp = requests.get(
        f"{SAVIO_BASE_URL}{path}",
        headers=_headers(),
        params=clean,
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise SavioError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise SavioError(f"GET {path}: respuesta no JSON: {resp.text[:200]}")


def _paginate_cursor(path: str, params: dict, items_key: str) -> Iterator[dict]:
    cursor = None
    for _ in range(MAX_PAGES):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        resp = _get(path, page_params)
        items = resp.get(items_key) or []
        for item in items:
            yield item
        cursor = resp.get("nextCursor")
        if not cursor:
            return
    raise SavioError(f"{path}: superó {MAX_PAGES} páginas (cursor)")


def _paginate_page(path: str, params: dict, items_key: str) -> Iterator[dict]:
    page = 1
    for _ in range(MAX_PAGES):
        page_params = dict(params)
        page_params["page"] = page
        resp = _get(path, page_params)
        items = resp.get(items_key) or []
        for item in items:
            yield item
        nxt = resp.get("next_page")
        if not nxt:
            return
        page = nxt
    raise SavioError(f"{path}: superó {MAX_PAGES} páginas (page)")


# ── Endpoints públicos ──────────────────────────────────────────────


def list_customers() -> list:
    """GET /customer — devuelve lista completa de clientes (sin paginación)."""
    data = _get("/customer")
    if isinstance(data, list):
        return data
    raise SavioError(f"/customer: esperaba lista, llegó {type(data).__name__}")


def list_subscriptions(limit: int = DEFAULT_LIMIT) -> Iterator[dict]:
    """GET /subscription/search — page-based, yields cada subscription."""
    return _paginate_page("/subscription/search", {"limit": limit}, "subscriptions")


def list_invoices(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> Iterator[dict]:
    """GET /invoice — cursor-based, soporta filtro por fecha (YYYY-MM-DD)."""
    return _paginate_cursor(
        "/invoice",
        {"limit": limit, "start_date": start_date, "end_date": end_date},
        "invoices",
    )


def list_payments(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> Iterator[dict]:
    """GET /payment — cursor-based, soporta filtro por fecha (YYYY-MM-DD)."""
    return _paginate_cursor(
        "/payment",
        {"limit": limit, "start_date": start_date, "end_date": end_date},
        "payments",
    )


def ping() -> dict:
    """Sanity check de auth. Pide la lista de customers (truncada al primer item)."""
    data = _get("/customer")
    if isinstance(data, list):
        return data[0] if data else {}
    return data


if __name__ == "__main__":
    import json

    cmd = sys.argv[1] if len(sys.argv) > 1 else "ping"

    def _dump_first(it, n=3):
        for i, r in enumerate(it):
            print(json.dumps(r, indent=2, ensure_ascii=False))
            if i + 1 >= n:
                break

    try:
        if cmd == "ping":
            print(json.dumps(ping(), indent=2, ensure_ascii=False))
        elif cmd == "customers":
            data = list_customers()
            print(f"[savio] total customers: {len(data)}")
            _dump_first(iter(data))
        elif cmd == "subscriptions":
            _dump_first(list_subscriptions())
        elif cmd == "invoices":
            _dump_first(list_invoices())
        elif cmd == "payments":
            _dump_first(list_payments())
        else:
            print(f"comando desconocido: {cmd}", file=sys.stderr)
            sys.exit(2)
    except SavioError as e:
        print(f"[savio] error: {e}", file=sys.stderr)
        sys.exit(1)
