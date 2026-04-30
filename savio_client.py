"""
Cliente HTTP para la API de Savio.

Solo lectura. Devuelve dicts crudos (sin mapear a modelos) — el mapeo
a SavioCustomer/SavioSubscription/SavioInvoice/SavioPayment vive en
el sync (paso 3), no acá.

Auth: header `Authorization: <API_KEY>` (token plano, sin "Bearer").
Paginación: `?page=N&per_page=M`. Iteramos hasta que la página vuelva
con menos de `per_page` registros.
"""

import os
import sys
from typing import Iterator, Optional

import requests

SAVIO_BASE_URL = os.getenv("SAVIO_BASE_URL", "https://api.savio.mx/api/v1")
SAVIO_API_KEY = os.getenv("SAVIO_API_KEY", "")

DEFAULT_PER_PAGE = 100
DEFAULT_TIMEOUT = 30


class SavioError(Exception):
    """Error genérico hablando con la API de Savio."""


def _headers() -> dict:
    if not SAVIO_API_KEY:
        raise SavioError("SAVIO_API_KEY no configurada en .env")
    return {
        "Authorization": SAVIO_API_KEY,
        "Accept": "application/json",
    }


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{SAVIO_BASE_URL}{path}"
    resp = requests.get(
        url,
        headers=_headers(),
        params=params or {},
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise SavioError(
            f"GET {path} -> {resp.status_code}: {resp.text[:300]}"
        )
    try:
        return resp.json()
    except ValueError:
        raise SavioError(f"GET {path} -> respuesta no es JSON: {resp.text[:200]}")


def _extract_records(data) -> list:
    """Saca la lista de registros de la respuesta. Soporta lista plana o
    los wrappers comunes (data/results/items/records)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "results", "items", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _paginate(
    path: str,
    params: Optional[dict] = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> Iterator[dict]:
    """Recorre todas las páginas y yielda un registro a la vez."""
    page = 1
    base_params = dict(params or {})
    while True:
        page_params = {**base_params, "page": page, "per_page": per_page}
        data = _get(path, page_params)
        records = _extract_records(data)
        if not records:
            return
        for r in records:
            yield r
        if len(records) < per_page:
            return
        page += 1


def _list(
    path: str,
    customer: Optional[str] = None,
    un: Optional[str] = None,
    per_page: int = DEFAULT_PER_PAGE,
    extra_params: Optional[dict] = None,
) -> Iterator[dict]:
    params: dict = {}
    if customer:
        params["customer"] = customer
    # NOTE: el nombre del parámetro UN está asumido como `un`.
    # Si Savio lo expone con otro nombre, cambiar acá.
    if un:
        params["un"] = un
    if extra_params:
        params.update(extra_params)
    return _paginate(path, params=params, per_page=per_page)


# ── Endpoints públicos ──────────────────────────────────────────────


def list_customers(
    customer: Optional[str] = None,
    un: Optional[str] = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> Iterator[dict]:
    return _list("/customer", customer=customer, un=un, per_page=per_page)


def list_subscriptions(
    customer: Optional[str] = None,
    un: Optional[str] = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> Iterator[dict]:
    return _list("/subscription", customer=customer, un=un, per_page=per_page)


def list_invoices(
    customer: Optional[str] = None,
    un: Optional[str] = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> Iterator[dict]:
    return _list("/invoice", customer=customer, un=un, per_page=per_page)


def list_payments(
    customer: Optional[str] = None,
    un: Optional[str] = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> Iterator[dict]:
    return _list("/payment", customer=customer, un=un, per_page=per_page)


def ping() -> dict:
    """Sanity check. Pide 1 customer; si auth funciona, vuelve un dict."""
    return _get("/customer", {"page": 1, "per_page": 1})


# ── Modo CLI para inspección manual ─────────────────────────────────
# Uso: python savio_client.py ping
#      python savio_client.py customers
#      python savio_client.py subscriptions
#      python savio_client.py invoices
#      python savio_client.py payments
if __name__ == "__main__":
    import json

    cmd = sys.argv[1] if len(sys.argv) > 1 else "ping"
    try:
        if cmd == "ping":
            print(json.dumps(ping(), indent=2, ensure_ascii=False))
        elif cmd == "customers":
            for i, r in enumerate(list_customers(per_page=5)):
                print(json.dumps(r, indent=2, ensure_ascii=False))
                if i >= 4:
                    break
        elif cmd == "subscriptions":
            for i, r in enumerate(list_subscriptions(per_page=5)):
                print(json.dumps(r, indent=2, ensure_ascii=False))
                if i >= 4:
                    break
        elif cmd == "invoices":
            for i, r in enumerate(list_invoices(per_page=5)):
                print(json.dumps(r, indent=2, ensure_ascii=False))
                if i >= 4:
                    break
        elif cmd == "payments":
            for i, r in enumerate(list_payments(per_page=5)):
                print(json.dumps(r, indent=2, ensure_ascii=False))
                if i >= 4:
                    break
        else:
            print(f"comando desconocido: {cmd}", file=sys.stderr)
            sys.exit(2)
    except SavioError as e:
        print(f"[savio] error: {e}", file=sys.stderr)
        sys.exit(1)
