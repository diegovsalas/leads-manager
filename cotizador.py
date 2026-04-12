# cotizador.py
"""
Generador de cotizaciones en PDF — Grupo Avantex
Replica el formato corporativo de Aromatex con soporte multi-marca.

Datos fiscales:
  Razón Social: LIMPIFEX
  RFC: LIM150325PV7
  Dirección: Lázaro Garza Ayala No. 110, Int. 110, Col. Tampiquito,
             66240, San Pedro Garza García, Monterrey, Nuevo León, MEX
  Teléfono: 8183354805
  Régimen Fiscal: 601 - General de Ley Personas Morales

Condiciones de pago:
  - Clientes clave: 30 días (PPD)
  - Clientes normales: PUE (pago en una exhibición)

Descuentos por volumen: Solo autorizan Alejandro Gil o Alan Aziz.

NOTA: Catálogos pendientes:
  - Aromatex: Alejandro Gil debe compartir
  - Pestex: Alan Aziz debe compartir
  - Weldex: Pendiente
"""
import io
import os
from datetime import datetime, timezone
from fpdf import FPDF

# ── Datos fiscales ──
EMPRESA = {
    "razon_social": "LIMPIFEX",
    "rfc": "LIM150325PV7",
    "direccion": "Lázaro Garza Ayala No. 110, Int. 110, Col. Tampiquito, 66240, San Pedro Garza García, Monterrey, Nuevo León, MEX",
    "telefono": "8183354805",
    "regimen": "601 - General de Ley Personas Morales",
    "banco": "BBVA Bancomer",
    "clabe": "012580001121151905",
    "cuenta": "0112115190",
}

# Colores por marca
MARCA_COLORS = {
    "Aromatex":      (128, 0, 128),   # Purple
    "Aromatex Home": (128, 0, 128),
    "Pestex":        (0, 100, 60),     # Green
    "Weldex":        (30, 60, 120),    # Blue
    "Nexo":          (50, 50, 50),     # Dark
}

MARCA_WEBS = {
    "Aromatex":      "www.aromatex.mx",
    "Aromatex Home": "www.aromatex.mx",
    "Pestex":        "www.pestex.mx",
    "Weldex":        "www.weldex.mx",
    "Nexo":          "www.grupoavantex.com",
}

IVA_RATE = 0.16

TERMINOS = [
    "Precio en pesos mexicanos.",
    "Enviar la cotización firmada por correo, indicando la aceptación del cliente a la propuesta realizada.",
    "La vigencia de esta cotización se especifica en el encabezado.",
    "Los precios no incluyen IVA, el cual se desglosa por separado.",
]


class CotizacionPDF(FPDF):
    def __init__(self, marca="Aromatex"):
        super().__init__()
        self.marca = marca
        self.color = MARCA_COLORS.get(marca, (128, 0, 128))
        self.web = MARCA_WEBS.get(marca, "www.grupoavantex.com")

    def header(self):
        pass  # Custom header in build

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Página {self.page_no()}/{{nb}}", align="C")


def generar_pdf(data):
    """
    Genera PDF de cotización.

    data = {
        "folio": "COT-0001",
        "fecha": "12-04-2026",
        "marca": "Aromatex",
        "nombre_cliente": "Victor Hugo Huerta",
        "empresa_cliente": "Empresa SA de CV",
        "direccion_cliente": "Calle 123, Col. Centro",
        "vendedor_nombre": "Azael Olivo",
        "condiciones_pago": "PUE",
        "vigencia_dias": 15,
        "items": [
            {
                "servicio": "Aroma Advance Pro",
                "descripcion": "Servicio de aromatización ambiental",
                "cantidad": 1,
                "frecuencia": "Cada 1 mes",
                "precio_unitario": 1860.00,
                "descuento_pct": 0,
            }
        ],
    }

    Returns: bytes (PDF content)
    """
    marca = data.get("marca", "Aromatex")
    pdf = CotizacionPDF(marca)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    color = MARCA_COLORS.get(marca, (128, 0, 128))
    web = MARCA_WEBS.get(marca, "www.grupoavantex.com")

    # ── Header: Logo area + date + web ──
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(*color)
    pdf.cell(80, 15, marca.upper(), ln=False)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    fecha = data.get("fecha", datetime.now(timezone.utc).strftime("%d-%m-%Y"))
    pdf.cell(60, 15, f"Fecha de envío: {fecha}", ln=False)

    # Web badge
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*color)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(45, 8, web, ln=True, align="C", fill=True)
    pdf.ln(2)

    # Line
    pdf.set_draw_color(*color)
    pdf.set_line_width(1)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Info boxes: Cliente / Generada por / Folio ──
    y_boxes = pdf.get_y()
    box_h = 22

    # Box 1: Cotización para
    pdf.set_draw_color(200, 200, 200)
    pdf.set_line_width(0.3)
    pdf.rect(10, y_boxes, 63, box_h)
    pdf.set_xy(12, y_boxes + 2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*color)
    pdf.cell(59, 4, "Cotización para", ln=True)
    pdf.set_x(12)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 30, 30)
    nombre = data.get("nombre_cliente", "")
    pdf.cell(59, 5, nombre[:35], ln=True)
    if data.get("empresa_cliente"):
        pdf.set_x(12)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(59, 4, data["empresa_cliente"][:40])

    # Box 2: Generada por
    pdf.rect(75, y_boxes, 55, box_h)
    pdf.set_xy(77, y_boxes + 2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*color)
    pdf.cell(51, 4, "Generada por", ln=True)
    pdf.set_x(77)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(51, 5, data.get("vendedor_nombre", ""), ln=True)

    # Box 3: Folio + Condiciones
    pdf.rect(132, y_boxes, 68, box_h)
    pdf.set_xy(134, y_boxes + 2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*color)
    pdf.cell(64, 4, "Folio / Condiciones", ln=True)
    pdf.set_x(134)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(64, 5, data.get("folio", ""), ln=True)
    pdf.set_x(134)
    pdf.set_font("Helvetica", "", 8)
    pago = data.get("condiciones_pago", "PUE")
    vigencia = data.get("vigencia_dias", 15)
    pdf.cell(64, 4, f"Pago: {pago} | Vigencia: {vigencia} días")

    pdf.set_y(y_boxes + box_h + 6)

    # ── Items table ──
    items = data.get("items", [])

    # Table header
    pdf.set_fill_color(*color)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    col_w = [35, 45, 15, 25, 22, 18, 22, 18]
    headers = ["Servicio", "Descripción", "Cant.", "Frecuencia", "Precio", "Desc%", "P. c/desc", "Total"]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 8, h, border=1, align="C", fill=True)
    pdf.ln()

    # Table rows
    pdf.set_text_color(30, 30, 30)
    pdf.set_font("Helvetica", "", 8)
    subtotal = 0
    descuento_total = 0

    for item in items:
        precio = float(item.get("precio_unitario", 0))
        cant = int(item.get("cantidad", 1))
        desc_pct = float(item.get("descuento_pct", 0))
        desc_monto = precio * (desc_pct / 100)
        precio_desc = precio - desc_monto
        total_item = precio_desc * cant
        subtotal += precio * cant
        descuento_total += desc_monto * cant

        row = [
            item.get("servicio", "")[:20],
            item.get("descripcion", "")[:28],
            str(cant),
            item.get("frecuencia", "")[:15],
            f"${precio:,.2f}",
            f"{desc_pct:.0f}%",
            f"${precio_desc:,.2f}",
            f"${total_item:,.2f}",
        ]
        for i, val in enumerate(row):
            align = "C" if i in (2, 5) else "R" if i >= 4 else "L"
            pdf.cell(col_w[i], 7, val, border=1, align=align)
        pdf.ln()

    pdf.ln(4)

    # ── Payment info + Totals side by side ──
    y_info = pdf.get_y()

    # Left: Payment info box
    pdf.set_draw_color(*color)
    pdf.set_line_width(0.5)
    pdf.rect(10, y_info, 95, 30)
    pdf.set_xy(14, y_info + 3)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*color)
    pdf.cell(87, 5, "Información de Pago", ln=True)
    pdf.set_x(14)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(87, 5, f"Banco: {EMPRESA['banco']}", ln=True)
    pdf.set_x(14)
    pdf.cell(87, 5, f"CLABE: {EMPRESA['clabe']}", ln=True)
    pdf.set_x(14)
    pdf.cell(87, 5, f"Núm. de Cuenta: {EMPRESA['cuenta']}", ln=True)

    # Right: Totals
    neto = subtotal - descuento_total
    iva = neto * IVA_RATE
    total = neto + iva

    pdf.set_xy(110, y_info)
    totals = [
        ("Subtotal", subtotal),
        ("I.V.A (16%)", iva),
        ("Descuento", descuento_total),
        ("Total", total),
    ]
    for label, val in totals:
        pdf.set_x(110)
        pdf.set_font("Helvetica", "B" if label == "Total" else "", 10)
        is_total = label == "Total"
        if is_total:
            pdf.set_fill_color(240, 240, 240)
        pdf.cell(45, 7, label, border=1, fill=is_total)
        pdf.cell(45, 7, f"${val:,.2f}", border=1, align="R", fill=is_total)
        pdf.ln()

    pdf.set_y(y_info + 36)

    # ── Terms ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*color)
    pdf.cell(0, 8, "Términos y Condiciones", ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(60, 60, 60)
    for i, t in enumerate(TERMINOS, 1):
        pdf.cell(0, 5, f"{i}. {t}", ln=True)

    # ── Output ──
    return pdf.output()


def folio_siguiente(db_session):
    """Genera folio incremental: COT-0001, COT-0002, etc."""
    from sqlalchemy import func
    from models import Cotizacion
    ultimo = db_session.query(func.max(Cotizacion.folio)).scalar()
    if ultimo and ultimo.startswith("COT-"):
        num = int(ultimo.split("-")[1]) + 1
    else:
        num = 1
    return f"COT-{num:04d}"
