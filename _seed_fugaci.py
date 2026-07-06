"""
Seed: cargar clientes de Fugaci en cs_accounts (Due Diligence).
Ejecutar UNA vez tras el deploy:
    python3 _seed_fugaci.py

FEAT-2026-07-06. Idempotente por nombre — si ya existe la cuenta, actualiza
el dd_metadata. Si no, la crea con en_due_diligence=True.

Los datos vienen del CSV que Diego pegó el 2026-07-06 en la conversación.
Se limpian caracteres UTF-8 mal-codificados del CSV original.
"""
import os
import sys
from decimal import Decimal, InvalidOperation

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://postgres.cyntwgxryfbrboehcdex:Brs99791avantex@aws-1-us-east-1.pooler.supabase.com:5432/postgres",
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from avantex_crm import create_app
from extensions import db
from models import CSAccount


def _to_dec(s):
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", ".").replace(" ", "").strip()
    # El CSV usa formato español "$ 124.980,00" — punto como miles, coma decimal
    # Al reemplazar "," → "." arriba invertimos, entonces reintentamos otro parseo
    try:
        return float(s)
    except (ValueError, InvalidOperation):
        try:
            # Formato español: "$ 124.980,00" → "124.980.00" → sacar todos los "." menos el último
            cleaned = str(s).replace("$", "").replace(" ", "").strip()
            # Reemplazar coma decimal → punto, y quitar puntos de miles
            if "," in cleaned:
                partes = cleaned.rsplit(",", 1)
                cleaned = partes[0].replace(".", "").replace(",", "") + "." + partes[1]
            return float(cleaned)
        except (ValueError, InvalidOperation):
            return None


def _clean(s):
    """Quita caracteres UTF-8 mal codificados del CSV."""
    if not s:
        return ""
    s = str(s)
    return (s.replace("Ã­", "í").replace("Ã¡", "á").replace("Ã©", "é")
             .replace("Ã³", "ó").replace("Ãº", "ú").replace("Ã±", "ñ")
             .replace("Ã‘", "Ñ").replace("Â", "").replace("SÃ­", "Sí")
             .strip())


# Datos del CSV (parseado del bloque que Diego pegó 2026-07-06)
# Formato: (nombre, precio, tipo_cliente, contacto_fugaci, alcance, credito,
#           comportamiento_pago, facturacion_ytd, cxc_junio, visitas_mes,
#           tiempo_visita, tecnicos, portal_url)
CLIENTES = [
    ("Metrorrey", 124980.00, "Recurrente", "Equipo Ventas", 374940.00, 124980.00, "Puntual", 15, "8 horas", 1),
    ("Solventum Planta Guadalupe", 31890.00, "Recurrente", "Equipo Ventas", 159450.00, 39312.40, "Puntual", 2, "8 horas", 1),
    ("TROPHE", 25631.00, "Recurrente", "Equipo Ventas", 122100.00, 29731.96, "Puntual", 3, "8 horas", 2),
    ("Solventum", 23584.00, "Recurrente", "Equipo Ventas", 98830.00, 29830.56, "Puntual", 8, "2-3 horas", 1),
    ("Deportivo San Agustín", 16000.00, "Recurrente", "Socios", 48000.00, 0, "Moderado", 4, "2-3 horas", 3),
    ("Masonite", 14204.52, "Recurrente", "Equipo Ventas", 71222.60, 16477.24, "Puntual", 2, "3-4 horas", 3),
    ("Parks Hospitality Monterrey", 10760.00, "Recurrente", "Equipo Ventas", 65636.00, 25587.28, "Moderado", 3, "4-6 horas", 2),
    ("PIMSA", 9734.00, "Recurrente", "Equipo Ventas", 48672.00, 33875.70, "Moroso", 2, "2 horas", 1),
    ("LASALLE PARTNERS (Torres Moradas, Fibra)", 7742.24, "Recurrente", "Equipo Ventas", 39920.10, 17962.00, "Puntual", 2, "5 horas + 1 hora", 3),
    ("Condominios De Las Huastecas", 6950.00, "Recurrente", "Equipo Ventas", 69950.00, 0, "Puntual", 1, "8 horas", 2),
    ("Solpack", 6448.28, "Recurrente", "Equipo Ventas", 32241.40, 14960.00, "Moderado", 2, "2 horas", 1),
    ("Nidec Motor", 6208.00, "Recurrente", "Equipo Ventas", 18624.00, 7201.28, "Puntual", 2, "2-6 horas", 3),
    ("Soriana Cumbres", 6202.00, "Recurrente", "Socios", 0, 0, "Moderado", 4, "2 horas", 1),
    ("Alimentos Calidad del Norte", 6000.00, "Recurrente", "Equipo Ventas", 18000.00, 13920.00, "Moroso", 2, "2 horas", 1),
    ("Tostadas Y botanas y Unidad de transporte", 5981.72, "Recurrente", "Equipo Ventas", 37090.32, 20449.00, "Puntual", 2, "3-4 horas", 1),
    ("CTR (Oficinas Administrativas)", 5805.34, "Recurrente", "Socios", 36665.24, 0, "Puntual", 2, "1-2 horas", 1),
    ("Leveth Medical", 5520.00, "Recurrente", "Equipo Ventas", 27600.00, 0, "Puntual", 1, "1 hora", 1),
    ("Torre Avalon", 5172.41, "Recurrente", "Equipo Ventas", 0, 6000.00, "Sin historial", 1, "4 horas", 2),
    ("Condominio Atria", 5148.00, "Recurrente", "Socios", 25740.00, 0, "Moderado", 1, "5 horas", 2),
    ("Condominio Belmont", 5000.00, "Bimestral", "Socios", 10500.00, 0, "Puntual", 1, "5 horas", 2),
    ("Nidec Global", 5000.00, "Recurrente", "Equipo Ventas", 25000.00, 0, "Puntual", 1, "4 horas", 2),
    ("TPS Armoring", 4935.00, "Recurrente", "Socios", 24675.00, 11449.20, "Moroso", 1, "3 horas", 1),
    ("Torre AXISS", 4896.00, "Recurrente", "Equipo Ventas", 24450.00, 5672.40, "Puntual", 1, "3 horas", 2),
    ("Torre Equus", 4840.50, "Recurrente", "Socios", 24202.50, 0, "Puntual", 1, "5 horas", 2),
    ("Nidec Planta 1", 4616.00, "Recurrente", "Equipo Ventas", 0, 5354.56, "Puntual", 1, "4 horas", 2),
    ("Torre Mun", 4375.00, "Recurrente", "Socios", 21875.00, 0, "Puntual", 1, "3 horas", 1),
    ("Torre Verona", 4147.50, "Recurrente", "Socios", 16590.00, 4811.10, "Puntual", 1, "5 horas", 2),
    ("Pork Rind", 3980.00, "Recurrente", "Equipo Ventas", 3980.00, 11344.80, "Sin historial", 2, "8 horas", 1),
    ("Las Fridas", 3513.80, "Recurrente", "Equipo Ventas", 17569.10, 0, "Puntual", 1, "5 horas", 2),
    ("Greif", 3382.00, "Recurrente", "Equipo Ventas", 11510.00, 3923.12, "Puntual", 2, "1 hora", 1),
    ("US PIPE", 3250.00, "Recurrente", "Equipo Ventas", 9750.00, 3770.00, "Moderado", 1, "1 hora", 1),
    ("Tres vientos", 3210.00, "Recurrente", "Equipo Ventas", 18450.00, 0, "Puntual", 1, "6 horas", 2),
    ("Torres Anida", 3200.00, "Recurrente", "Equipo Ventas", 12800.00, 0, "Puntual", 1, "5 horas", 2),
    ("Plaza Kerkus", 3150.00, "Recurrente", "Socios", 15750.00, 0, "Puntual", 1, "2 horas", 1),
    ("Plaza Oasis", 2950.00, "Recurrente", "Equipo Ventas", 15325.00, 0, "Moderado", 1, "2 horas", 1),
    ("Torre Agatta", 2919.00, "Recurrente", "Socios", 0, 0, "Sin historial", 1, "5 horas", 2),
    ("Treinta Comercializadora (Saltillo)", 2683.00, "Recurrente", "Equipo Ventas", 13415.00, 0, "Puntual", 1, "30 minutos", 1),
    ("URVET", 2630.00, "Recurrente", "Equipo Ventas", 10520.00, 0, "Puntual", 1, "1 hora", 1),
    ("Yooju Foods", 2532.00, "Recurrente", "Equipo Ventas", 12660.00, 2937.12, "Moderado", 2, "1 hora", 1),
    ("Centro Escolar Gante", 2420.00, "Recurrente", "Equipo Ventas", 12100.00, 0, "Puntual", 1, "3 horas", 1),
    ("JUYOUNG SERVICES SA DE CV", 2400.00, "Recurrente", "Equipo Ventas", 12000.00, 0, "Moderado", 1, "1 hora", 1),
    ("Zari", 2400.00, "Recurrente", "Equipo Ventas", 7000.00, 0, "Moderado", 1, "3 horas", 1),
    ("AVE Hospital", 2331.00, "Recurrente", "Equipo Ventas", 6993.00, 2703.96, "Puntual", 1, "1 hora", 1),
    ("Soriana Valle Poniente", 2100.00, "Recurrente", "Socios", 6300.00, 4872.00, "Moderado", 1, "1 hora", 1),
    ("Apollo Winebar", 2000.00, "Recurrente", "Equipo Ventas", 10600.00, 2320.00, "Puntual", 1, "1 hora", 1),
    ("Koprimo", 1980.00, "Recurrente", "Equipo Ventas", 24920.00, 0, "Puntual", 1, "1 hora", 1),
    ("Barrio Med", 1980.00, "Recurrente", "Equipo Ventas", 3960.00, 2296.80, "Moderado", 1, "2 horas", 1),
    ("Havana", 1950.00, "Recurrente", "Equipo Ventas", 9750.00, 0, "Puntual", 2, "1 hora", 1),
    ("CTR (Cedis)", 1866.66, "Recurrente", "Socios", 9706.65, 0, "Puntual", 2, "3+1 horas", 1),
    ("Barmesa Planta 2", 1825.00, "Recurrente", "Equipo Ventas", 7300.00, 2117.00, "Puntual", 1, "1 hora", 1),
    ("Ana Cecilia Ibarra de la Garza", 1800.00, "Recurrente", "Equipo Ventas", 7200.00, 0, "Puntual", 1, "2 horas", 1),
    ("LASALLE PARTNERS (Edificio Tamayo)", 1794.00, "Recurrente", "Equipo Ventas", 16162.27, 10116.08, "Puntual", 1, "2 horas", 1),
    ("Barmesa Pumps Planta 1", 1785.00, "Recurrente", "Socios", 7140.00, 2070.60, "Puntual", 1, "1 hora", 1),
    ("Soriana Colon", 1719.00, "Recurrente", "Socios", 22515.00, 12061.68, "Moderado", 4, "1 hora", 1),
    ("CIMSAMEX", 1712.44, "Recurrente", "Socios", 20549.31, 0, "Puntual", 1, "2 horas", 1),
    ("Taquería La Capital", 1700.00, "Recurrente", "Equipo Ventas", 8500.00, 0, "Puntual", 1, "1 hora", 1),
    ("General de productos para el agua", 1659.00, "Recurrente", "Equipo Ventas", 8295.00, 0, "Puntual", 1, "1 hora", 1),
    ("Quality Post (Almacen)", 1627.50, "Recurrente", "Socios", 11246.50, 3775.80, "Moderado", 1, "2 horas", 1),
    ("Quality Post (Oficinas)", 1627.50, "Recurrente", "Socios", 11246.50, 3775.80, "Moderado", 1, "30 minutos", 1),
    ("Boru (Cedis)", 1600.00, "Recurrente", "Socios", 8000.00, 1856.00, "Puntual", 1, "1 hora", 1),
    ("Maut", 1600.00, "Recurrente", "Equipo Ventas", 8000.00, 1856.00, "Puntual", 1, "1 hora", 1),
    ("Frutas y Legumbres", 1600.00, "Recurrente", "Equipo Ventas", 4800.00, 0, "Puntual", 1, "1 hora", 1),
    ("Clara Quintanilla", 1594.00, "Recurrente", "Socios", 0, 0, "Puntual", 1, "3 horas", 1),
    ("David Wolberg", 1500.00, "Recurrente", "Equipo Ventas", 9000.00, 0, "Puntual", 1, "1 hora", 1),
    ("Plaza Loreto", 1500.00, "Recurrente", "Socios", 4650.00, 0, "Puntual", 1, "2 horas", 1),
    ("Stural", 1500.00, "Recurrente", "Equipo Ventas", 9000.00, 0, "Puntual", 1, "1 hora", 1),
    ("Tecnologias en extrusion", 1500.00, "Bimestral", "Socios", 4500.00, 0, "Puntual", 1, "1 hora", 1),
    ("Birrieria Monterrey (Cedis)", 1400.00, "Recurrente", "Equipo Ventas", 7000.00, 1624.00, "Moderado", 1, "1 hora", 1),
    ("Soluciones de Energia AGIT", 1400.00, "Recurrente", "Equipo Ventas", 5600.00, 0, "Puntual", 1, "1 hora", 1),
    ("PVC GLOBAL", 1365.00, "Recurrente", "Socios", 8190.00, 3880.20, "Puntual", 1, "2 horas", 1),
    ("Picachos", 1350.00, "Recurrente", "Equipo Ventas", 6750.00, 1566.00, "Puntual", 2, "30 minutos", 1),
    ("The pollock", 1350.00, "Recurrente", "Equipo Ventas", 6750.00, 1566.00, "Puntual", 2, "30 minutos", 1),
    ("Plaza Los Rios", 1341.90, "Recurrente", "Socios", 6709.50, 1556.60, "Puntual", 1, "1 hora", 1),
    ("TDA", 1312.50, "Recurrente", "Socios", 6562.50, 1522.50, "Moderado", 1, "1 hora", 1),
    ("Montebello", 1300.00, "Eventual", "Socios", 2600.00, 0, "Puntual", 1, "1 hora", 1),
    ("COTEMAR", 1285.00, "Eventual", "Socios", 1185.00, 0, "Puntual", 1, "1 hora", 1),
    ("Treinta Comercializadora (Guadalupe)", 1283.00, "Recurrente", "Equipo Ventas", 6415.00, 1488.28, "Puntual", 1, "1 hora", 1),
    ("Adisa y Unidad de transporte", 1280.00, "Recurrente", "Equipo Ventas", 7680.00, 0, "Moderado", 1, "1 hora", 1),
    ("IENU (Cumbres)", 1260.00, "Recurrente", "Socios", 4250.00, 0, "Puntual", 1, "1 hora", 1),
    ("Eme de Emilia (Taller)", 1250.00, "Recurrente", "Socios", 6250.00, 0, "Puntual", 1, "40 minutos", 1),
    ("Eme de Emilia (Tienda)", 1250.00, "Recurrente", "Socios", 6250.00, 0, "Puntual", 1, "40 minutos", 1),
    ("Previsiones 2000 (Crematorio)", 1200.00, "Bimestral", "Equipo Ventas", 3600.00, 0, "Puntual", 1, "1 hora", 1),
    ("Quesadillas de la abuela (Barrio antiguo)", 1200.00, "Recurrente", "Equipo Ventas", 5650.00, 0, "Puntual", 1, "1 hora", 1),
    ("SB Padel", 1200.00, "Recurrente", "Equipo Ventas", 6000.00, 0, "Puntual", 1, "1 hora", 1),
    ("Ultrablue", 1200.00, "Recurrente", "Socios", 0, 0, "Moderado", 1, "40 minutos", 1),
    ("Cardiolink Clin Trials", 1200.00, "Eventual", "Equipo Ventas", 2400.00, 0, "Puntual", 1, "1 hora", 1),
    ("Juan Manuel Caballero", 1200.00, "Recurrente", "Equipo Ventas", 1200.00, 0, "Puntual", 1, "1 hora", 1),
    ("Birrieria Monterrey (Carretera N)", 1150.00, "Recurrente", "Equipo Ventas", 5750.00, 1334.00, "Moderado", 1, "1 hora", 1),
    ("Birrieria Monterrey (San Nicolas)", 1150.00, "Recurrente", "Equipo Ventas", 5750.00, 1334.00, "Moderado", 1, "1 hora", 1),
    ("COMASA", 1150.00, "Recurrente", "Equipo Ventas", 4600.00, 1334.00, "Moroso", 1, "1 hora", 1),
    ("VC999 Packaging Systems", 1144.00, "Recurrente", "Equipo Ventas", 4576.00, 0, "Puntual", 1, "1 hora", 1),
    ("Poly Proveedor", 1100.00, "Recurrente", "Equipo Ventas", 5500.00, 0, "Puntual", 1, "1 hora", 1),
    ("Avalia Mall del Valle", 1080.00, "Recurrente", "Equipo Ventas", 5400.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Sersal Oficina y Unidad de transporte", 1065.00, "Recurrente", "Socios", 90860.00, 15648.40, "Puntual", 1, "1 hora", 1),
    ("Plaza Bolivar", 1023.75, "Recurrente", "Socios", 4258.80, 0, "Puntual", 1, "1 hora", 1),
    ("Quesadillas de la abuela (Valle)", 1000.00, "Recurrente", "Equipo Ventas", 6000.00, 0, "Puntual", 1, "1 hora", 1),
    ("Salsa Pa Todo", 1000.00, "Recurrente", "Socios", 6250.00, 0, "Puntual", 1, "30 minutos", 1),
    ("American Airlines", 988.00, "Recurrente", "Equipo Ventas", 5776.00, 2292.16, "Puntual", 1, "30 minutos", 1),
    ("Panaderia sanchez", 983.00, "Recurrente", "Equipo Ventas", 5898.00, 0, "Puntual", 1, "1 hora", 1),
    ("Birrieria Monterrey (Guadalupe)", 980.00, "Recurrente", "Equipo Ventas", 4900.00, 1136.80, "Moderado", 1, "1 hora", 1),
    ("Birrieria Monterrey (Roma)", 980.00, "Recurrente", "Equipo Ventas", 4900.00, 1136.80, "Moderado", 1, "1 hora", 1),
    ("Celupal Cedis", 980.00, "Recurrente", "Equipo Ventas", 1960.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Pyrolac", 977.00, "Recurrente", "Equipo Ventas", 4885.00, 0, "Puntual", 1, "1 hora", 1),
    ("VSF", 968.00, "Recurrente", "Socios", 11626.08, 0, "Puntual", 1, "30 minutos", 1),
    ("CTR (San Fernando)", 936.00, "Recurrente", "Socios", 2808.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Avalia Revolucion", 900.00, "Recurrente", "Equipo Ventas", 4500.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Aduax (Claudia Nader)", 850.00, "Recurrente", "Socios", 850.00, 0, "Puntual", 1, "1 hora", 1),
    ("Denisse Aguilar", 850.00, "Recurrente", "Directo Fugaci", 5100.00, 0, "Puntual", 1, "1 hora", 1),
    ("IENU (Leones)", 850.00, "Recurrente", "Socios", 7560.00, 0, "Puntual", 1, "1 hora", 1),
    ("Proyectos Unisude", 850.00, "Recurrente", "Socios", 3400.00, 986.00, "Moderado", 1, "1 hora", 1),
    ("Importek Ramos Arizpe", 850.00, "Eventual", "Socios", 0, 0, "Moderado", 1, "30 minutos", 1),
    ("Importek", 816.31, "Recurrente", "Socios", 0, 0, "Moderado", 1, "30 minutos", 1),
    ("Andres Serna", 800.00, "Cuatrimestral", "Socios", 800.00, 0, "Puntual", 1, "1 hora", 1),
    ("Boru (Colorines)", 800.00, "Recurrente", "Socios", 4000.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Boru (Cumbres)", 800.00, "Recurrente", "Socios", 4000.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Boru (Gomez Morin)", 800.00, "Recurrente", "Socios", 4000.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Boru (Mision del Valle)", 800.00, "Recurrente", "Socios", 4000.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Boru (San Jeronimo)", 800.00, "Recurrente", "Socios", 4000.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Boru (Carretera Nacional)", 800.00, "Recurrente", "Socios", 4000.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Boru (Arboleda)", 800.00, "Recurrente", "Socios", 800.00, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Chiso", 800.00, "Recurrente", "Socios", 3200.00, 0, "Puntual", 1, "1 hora", 1),
    ("Laboratorio", 800.00, "Bimestral", "Equipo Ventas", 0, 928.00, "Puntual", 1, "30 minutos", 1),
    ("Laboratorios Monterrey", 787.50, "Recurrente", "Equipo Ventas", 2437.50, 0, "Puntual", 1, "30 minutos", 1),
    ("Central Capitalia", 780.00, "Bimestral", "Equipo Ventas", 1560.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Marigot Quintanilla", 750.00, "Eventual", "Socios", 750.00, 0, "Puntual", 1, "1 hora", 1),
    ("Azul Quedito", 750.00, "Recurrente", "Equipo Ventas", 750.00, 0, "Moderado", 1, "30 minutos", 1),
    ("Celupal", 700.00, "Recurrente", "Equipo Ventas", 700.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Marcela Salaman", 700.00, "Recurrente", "Socios", 4200.00, 0, "Puntual", 1, "1 hora", 1),
    ("Jose Gonzalez", 700.00, "Recurrente", "Socios", 700.00, 0, "Puntual", 1, "1 hora", 1),
    ("Bodega Pescaderia Lamar", 700.00, "Recurrente", "Equipo Ventas", 0, 0, "Moderado", 1, "30 minutos", 1),
    ("Bodega Salchichoneria Cuatro Quesos", 700.00, "Recurrente", "Equipo Ventas", 0, 0, "Moderado", 1, "30 minutos", 1),
    ("Bodega Pescaderia Lamar Cumbres", 700.00, "Recurrente", "Equipo Ventas", 0, 0, "Puntual", 1, "30 minutos", 1),
    ("Consultorio Cisneros", 650.00, "Trimestral", "Socios", 0, 0, "Puntual", 1, "30 minutos", 1),
    ("Previsiones 2000 (Escobedo)", 680.00, "Bimestral", "Equipo Ventas", 2040.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Previsiones 2000 (Guadalupe)", 680.00, "Bimestral", "Equipo Ventas", 2040.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Previsiones 2000 (Mitras)", 680.00, "Bimestral", "Equipo Ventas", 2040.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Dileo", 630.00, "Recurrente", "Equipo Ventas", 7560.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Mauricio Martinez", 550.00, "Eventual", "Socios", 0, 0, "Puntual", 1, "1 hora", 1),
    ("Alejandro Lobeira", 550.00, "Recurrente", "Socios", 0, 0, "Puntual", 1, "1 hora", 1),
    ("Barmesa Genman", 500.00, "Recurrente", "Equipo Ventas", 2000.00, 580.00, "Puntual", 1, "1 hora", 1),
    ("Bikemarket", 470.40, "Recurrente", "Socios", 1881.60, 545.66, "Moderado", 1, "30 minutos", 1),
    ("Ana Laura Maldonado", 437.50, "Recurrente", "Socios", 4025.00, 0, "Moderado", 1, "1 hora", 1),
    ("Previsiones 2000 (Diego Capillas)", 360.00, "Bimestral", "Equipo Ventas", 1080.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Previsiones 2000 (Diego Plus)", 360.00, "Bimestral", "Equipo Ventas", 1080.00, 0, "Puntual", 1, "30 minutos", 1),
    ("Gabino Ortega", 350.00, "Recurrente", "Directo Fugaci", 1750.00, 0, "Puntual", 1, "15 minutos", 1),
    ("Avalia Soles", 350.00, "Recurrente", "Equipo Ventas", 1750.00, 0, "Puntual", 1, "15 minutos", 1),
    ("Star del Norte Unidades de transporte", 350.00, "Recurrente", "Equipo Ventas", 105847.00, 11716.00, "Moderado", 1, "15 minutos", 1),
]


def main():
    app = create_app()
    with app.app_context():
        creadas = 0
        actualizadas = 0
        for row in CLIENTES:
            (nombre, precio, tipo_cliente, contacto, fact_ytd, cxc,
             comport, visitas, tiempo, tecnicos) = row
            nombre = _clean(nombre)
            metadata = {
                "precio": precio,
                "tipo_cliente": tipo_cliente,
                "contacto_fugaci": _clean(contacto),
                "facturacion_ytd": fact_ytd,
                "cxc_junio": cxc,
                "comportamiento_pago": _clean(comport),
                "visitas_mes": visitas,
                "tiempo_visita": _clean(tiempo),
                "tecnicos": tecnicos,
                "contrato_vigente": True,
            }
            existing = CSAccount.query.filter_by(nombre=nombre).first()
            if existing:
                existing.en_due_diligence = True
                existing.origen_adquisicion = "Fugaci"
                existing.dd_metadata = metadata
                existing.unidades_contratadas = existing.unidades_contratadas or "PESTEX"
                actualizadas += 1
            else:
                acc = CSAccount(
                    nombre=nombre,
                    kam_id=None,               # sin KAM asignado aún
                    en_due_diligence=True,
                    origen_adquisicion="Fugaci",
                    dd_metadata=metadata,
                    unidades_contratadas="PESTEX",
                    mrr=0,                     # NO se computa hasta promocionar
                    mrr_observado=0,
                    arr_proyectado=0,
                    sucursales=0,
                )
                db.session.add(acc)
                creadas += 1
        db.session.commit()
        print(f"✓ Seed Fugaci: {creadas} creadas, {actualizadas} actualizadas")
        print(f"  Total en Due Diligence ahora: {CSAccount.query.filter_by(en_due_diligence=True).count()}")


if __name__ == "__main__":
    main()
