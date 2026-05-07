"""
Reporte MRR Savio.

Port del mrrReport() de vendedores.cloud/savio.js. Consume las tablas
savio_* + customer_master/rfcs y devuelve un dict con:
  - mrr_total / mrr_nuevo / mrr_upsell / mrr_existente
  - mrr_por_un (por unidad)
  - eventual_total, comercializadora, refacturaciones (de invoices)
  - desglose_por_un con buckets nuevo/upsell/existente y subdivisiones
  - active_clients_by_un + total_active_clients
  - ventas_por_un (invoice totals con IVA)
  - total_facturado_del_mes (con + sin IVA y desglose completo por UN+tipo)

La regla de "nuevo vs upsell vs existente" compara MRR del mes actual con
el del mes previo, agrupado por master_id (o por RFC si no hay master).
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from extensions import db


def _date_range(month: Optional[str]) -> tuple[str, str]:
    if month and len(month) == 7 and month[4] == "-":
        try:
            y, m = int(month[:4]), int(month[5:])
            start = date(y, m, 1)
            end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
            return start.isoformat(), end.isoformat()
        except ValueError:
            pass
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=30)).isoformat(), today.isoformat()


def _prev_month_range(start_date: str) -> tuple[str, str]:
    y, m = int(start_date[:4]), int(start_date[5:7])
    pm = 12 if m == 1 else m - 1
    py = y - 1 if m == 1 else y
    prev_start = date(py, pm, 1)
    prev_end = (date(py, pm + 1, 1) if pm < 12 else date(py + 1, 1, 1)) - timedelta(days=1)
    return prev_start.isoformat(), prev_end.isoformat()


def _client_mrr_for_month(start: str, end: str) -> dict:
    """Devuelve {ckey: {total, by_unit: {unit: {mrr, weldex_sub}}}}.
    ckey = master_id si existe, si no 'rfc:<tax_id>'."""
    sql = text("""
        SELECT ss.unit, ss.uen, ss.description, ss.customer_id,
               ss.mrr,
               COALESCE(cr.master_id::text, 'rfc:' || sc.tax_id) AS ckey
        FROM savio_subscriptions ss
        JOIN savio_customers sc ON sc.customer_id = ss.customer_id
        LEFT JOIN customer_rfcs cr ON cr.rfc = sc.tax_id
        WHERE ss.mrr > 0
          AND ss.start_date <= :end_d
          AND (ss.contract_end_date IS NULL OR ss.contract_end_date > :start_d)
    """)
    rows = db.session.execute(sql, {"start_d": start, "end_d": end}).fetchall()
    result = {}
    for r in rows:
        ckey = r.ckey
        if ckey not in result:
            result[ckey] = {"total": 0.0, "by_unit": {}}
        c = result[ckey]
        mrr = float(r.mrr or 0)
        c["total"] += mrr
        u = r.unit or "sin_clasificar"
        prev = c["by_unit"].get(u) or {"mrr": 0.0, "weldex_sub": None}
        prev["mrr"] += mrr
        if u == "weldex":
            prev["weldex_sub"] = "intendencia" if r.uen == "QTB" else "weldex_recurrente"
        c["by_unit"][u] = prev
    return result


def _empty_bucket():
    return {"mrr_total": 0.0, "mrr_nuevos": 0.0, "mrr_existente": 0.0,
            "mrr_upsell": 0.0, "count_nuevos": 0, "count": 0}


def mrr_report(month: Optional[str] = None) -> dict:
    start_date, end_date = _date_range(month)
    prev_start, prev_end = _prev_month_range(start_date)

    curr_clients = _client_mrr_for_month(start_date, end_date)
    prev_clients = _client_mrr_for_month(prev_start, prev_end)

    desglose: dict = {}
    weldex_subs: dict = {}
    mrr_total = 0.0
    mrr_nuevo = 0.0
    mrr_upsell = 0.0
    mrr_existente = 0.0
    nuevos_count = 0

    for ckey, curr in curr_clients.items():
        prev = prev_clients.get(ckey)
        for unit, cu in curr["by_unit"].items():
            bucket = desglose.setdefault(unit, _empty_bucket())
            bucket["mrr_total"] += cu["mrr"]
            bucket["count"] += 1
            mrr_total += cu["mrr"]

            prev_unit_mrr = (prev["by_unit"].get(unit, {}).get("mrr", 0.0)) if prev else 0.0
            if prev_unit_mrr == 0:
                bucket["mrr_nuevos"] += cu["mrr"]
                bucket["count_nuevos"] += 1
                mrr_nuevo += cu["mrr"]
                nuevos_count += 1
                tipo = "nuevo"
            elif cu["mrr"] > prev_unit_mrr:
                diff = cu["mrr"] - prev_unit_mrr
                bucket["mrr_upsell"] += diff
                mrr_upsell += diff
                bucket["mrr_existente"] += prev_unit_mrr
                mrr_existente += prev_unit_mrr
                tipo = "upsell"
            else:
                bucket["mrr_existente"] += cu["mrr"]
                mrr_existente += cu["mrr"]
                tipo = "existente"

            # Sub-breakdown weldex
            sk = cu.get("weldex_sub")
            if unit == "weldex" and sk:
                ws = weldex_subs.setdefault(sk, _empty_bucket())
                ws["mrr_total"] += cu["mrr"]
                ws["count"] += 1
                if tipo == "nuevo":
                    ws["mrr_nuevos"] += cu["mrr"]
                    ws["count_nuevos"] += 1
                elif tipo == "upsell":
                    ws["mrr_upsell"] += cu["mrr"] - prev_unit_mrr
                    ws["mrr_existente"] += prev_unit_mrr
                else:
                    ws["mrr_existente"] += cu["mrr"]

    mrr_por_un = {u: d["mrr_total"] for u, d in desglose.items()}

    # Active clients by unit (distinct master_id)
    active_sql = text("""
        SELECT ss.unit, COUNT(DISTINCT COALESCE(cr.master_id::text, sc.customer_id)) AS cnt
        FROM savio_subscriptions ss
        JOIN savio_customers sc ON sc.customer_id = ss.customer_id
        LEFT JOIN customer_rfcs cr ON cr.rfc = sc.tax_id
        WHERE ss.mrr > 0 AND ss.start_date <= :end_d
          AND (ss.contract_end_date IS NULL OR ss.contract_end_date > :start_d)
        GROUP BY ss.unit
    """)
    active_rows = db.session.execute(active_sql, {"start_d": start_date, "end_d": end_date}).fetchall()
    active_clients_by_un = {r.unit: int(r.cnt) for r in active_rows if r.unit}
    total_active_clients = sum(active_clients_by_un.values())

    # Eventual + Poliza por unit (de invoices)
    evt_sql = text("""
        SELECT unit, type, COALESCE(SUM(amount), 0) AS v
        FROM savio_invoices
        WHERE type IN ('eventual', 'poliza') AND date BETWEEN :s AND :e
        GROUP BY unit, type
    """)
    evt_rows = db.session.execute(evt_sql, {"s": start_date, "e": end_date}).fetchall()
    eventual_map: dict = {}
    poliza_map: dict = {}
    for r in evt_rows:
        if r.type == "eventual":
            eventual_map[r.unit] = float(r.v or 0)
        else:
            poliza_map[r.unit] = float(r.v or 0)
    all_units = set(eventual_map) | set(poliza_map)
    by_unit_eventual = []
    for u in all_units:
        if u not in desglose:
            desglose[u] = _empty_bucket()
        desglose[u]["eventual"] = eventual_map.get(u, 0.0)
        desglose[u]["poliza"] = poliza_map.get(u, 0.0)
        by_unit_eventual.append({"unit": u, "v": eventual_map.get(u, 0.0) + poliza_map.get(u, 0.0)})
    eventual_total = sum(r["v"] for r in by_unit_eventual)

    # Weldex eventual de invoices
    w_evt = db.session.execute(text("""
        SELECT COALESCE(SUM(amount), 0) AS v FROM savio_invoices
        WHERE unit='weldex' AND type='eventual' AND date BETWEEN :s AND :e
    """), {"s": start_date, "e": end_date}).scalar() or 0
    weldex_subs.setdefault("weldex_eventual", _empty_bucket())["eventual"] = float(w_evt)
    if "weldex" in desglose:
        desglose["weldex"]["subs"] = weldex_subs

    # Refacturaciones (negativas)
    refac_rows = db.session.execute(text("""
        SELECT amount FROM savio_invoices
        WHERE type='refacturacion' AND date BETWEEN :s AND :e
    """), {"s": start_date, "e": end_date}).fetchall()
    refacturaciones = sum(float(r.amount) for r in refac_rows if r.amount and float(r.amount) < 0)

    # Comercializadora
    comercializadora = db.session.execute(text("""
        SELECT COALESCE(SUM(amount), 0) AS v FROM savio_invoices
        WHERE unit='comercializadora' AND date BETWEEN :s AND :e
    """), {"s": start_date, "e": end_date}).scalar() or 0

    # Ventas por UN+type (invoice chart con IVA)
    ventas_por_un = [
        {"unit": r.unit, "type": r.type, "total": float(r.total or 0), "n": int(r.n)}
        for r in db.session.execute(text("""
            SELECT unit, type, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n
            FROM savio_invoices
            WHERE date BETWEEN :s AND :e AND unit IS NOT NULL
            GROUP BY unit, type ORDER BY unit, type
        """), {"s": start_date, "e": end_date}).fetchall()
    ]

    # Total facturado del mes (con IVA)
    fac_total = float(db.session.execute(text("""
        SELECT COALESCE(SUM(amount), 0) FROM savio_invoices WHERE date BETWEEN :s AND :e
    """), {"s": start_date, "e": end_date}).scalar() or 0)

    fac_desglose_row = db.session.execute(text("""
        SELECT
          COALESCE(SUM(CASE WHEN unit='aromatex' AND sum_mrr=true THEN amount END), 0) AS aromatex_recurrente,
          COALESCE(SUM(CASE WHEN unit='aromatex' AND type='eventual' THEN amount END), 0) AS aromatex_eventual,
          COALESCE(SUM(CASE WHEN unit='aromatex' AND type='poliza' THEN amount END), 0) AS aromatex_poliza,
          COALESCE(SUM(CASE WHEN unit='aromatex' AND type='refacturacion' THEN amount END), 0) AS aromatex_refacturacion,
          COALESCE(SUM(CASE WHEN unit='pestex' AND sum_mrr=true THEN amount END), 0) AS pestex_recurrente,
          COALESCE(SUM(CASE WHEN unit='pestex' AND type='eventual' THEN amount END), 0) AS pestex_eventual,
          COALESCE(SUM(CASE WHEN unit='pestex' AND type='poliza' THEN amount END), 0) AS pestex_poliza,
          COALESCE(SUM(CASE WHEN unit='weldex' AND (sub='intendencia' OR uen='QTB') THEN amount END), 0) AS weldex_intendencia,
          COALESCE(SUM(CASE WHEN unit='weldex' AND (sub='weldex_recurrente' OR (uen='WELDEX' AND type='recurrente')) THEN amount END), 0) AS weldex_recurrente,
          COALESCE(SUM(CASE WHEN unit='weldex' AND (sub='weldex_eventual' OR (uen='WELDEX' AND type='eventual')) THEN amount END), 0) AS weldex_eventual,
          COALESCE(SUM(CASE WHEN unit='weldu' THEN amount END), 0) AS weldu,
          COALESCE(SUM(CASE WHEN unit='comercializadora' THEN amount END), 0) AS comercializadora,
          COALESCE(SUM(CASE WHEN unit IS NULL THEN amount END), 0) AS sin_clasificar
        FROM savio_invoices WHERE date BETWEEN :s AND :e
    """), {"s": start_date, "e": end_date}).fetchone()
    fac_desglose = {k: float(getattr(fac_desglose_row, k) or 0) for k in fac_desglose_row._fields} if fac_desglose_row else {}

    return {
        "month": month or f"{start_date}..{end_date}",
        "range": {"start_date": start_date, "end_date": end_date},
        "mrr_total": mrr_total,
        "mrr_nuevo": mrr_nuevo,
        "mrr_upsell": mrr_upsell,
        "mrr_existente": mrr_existente,
        "mrr_por_un": mrr_por_un,
        "eventual_total": eventual_total,
        "eventual_por_un": {r["unit"]: r["v"] for r in by_unit_eventual},
        "comercializadora": float(comercializadora),
        "refacturaciones": refacturaciones,
        "desglose_por_un": desglose,
        "active_clients_by_un": active_clients_by_un,
        "total_active_clients": total_active_clients,
        "ventas_por_un": ventas_por_un,
        "nuevos_count": nuevos_count,
        "total_facturado_del_mes": {
            "total": fac_total,
            "total_sin_iva": round(fac_total / 1.16, 2),
            "desglose": fac_desglose,
        },
    }
