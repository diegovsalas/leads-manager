"""
Evidencia de cierre ganado.

FEAT-2026-09-08: Pestex no factura a mes corriente. Buena parte se vende
con crédito a 30 días, así que cuando el vendedor mueve el trato a Cerrado
Ganado todavía no existe factura ni folio que respalde la venta. El modal
de cierre lo permitía ("puedes cerrar igual y completar después"), y esas
ventas quedaban sin ningún respaldo hasta que llegara el CFDI — o para
siempre, si nunca llegaba.

Aquí vive la regla: para cerrar un trato de Pestex el vendedor declara con
qué confirma la venta (1 o 2 de los cinco respaldos aceptados) y sube el
archivo de cada uno. Una evidencia marcada sin archivo no cuenta.

Puro sobre modelos: no define rutas. Lo consumen blueprints/leads.py y
blueprints/oportunidades.py, que son las dos puertas a Cerrado Ganado.
"""
import os
import uuid

from flask import current_app

from extensions import db
from models import CierreEvidencia

# Bucket propio: la evidencia de venta tiene otra vida útil y otra audiencia
# que las fotos de tickets o los PDF de facturas de incidencias.
BUCKET = "cierre-evidencias"

MAX_BYTES = 10 * 1024 * 1024          # 10 MB por archivo
MAX_ARCHIVOS_POR_TIPO = 5

MIMES_PERMITIDOS = (
    "application/pdf",
    "image/jpeg", "image/png", "image/webp", "image/heic",
    # Órdenes de compra y contratos llegan seguido en Office.
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

TIPOS = CierreEvidencia.TIPOS
MAX_TIPOS = CierreEvidencia.MAX_TIPOS

# Unidades de negocio que exigen evidencia para cerrar. Se compara en
# minúsculas y por substring, porque marca_interes trae variantes
# ("Pestex", "PESTEX RECURRENTE", "pestex polizas").
UNIDADES_CON_EVIDENCIA = ("pestex",)


def requiere_evidencia(unidad) -> bool:
    """¿Esta unidad de negocio exige evidencia para cerrar como ganado?

    Acepta un string o una colección de unidades. Con una colección basta
    que UNA la exija: un lead multi UN que trae Pestex mueve dinero de
    Pestex, aunque la unidad dueña del trato sea otra.
    """
    if unidad is None:
        return False
    unidades = [unidad] if isinstance(unidad, str) else list(unidad)
    for u in unidades:
        texto = (u or "").strip().lower()
        if any(marca in texto for marca in UNIDADES_CON_EVIDENCIA):
            return True
    return False


def _get_storage():
    """Cliente de Supabase Storage, o None si no está configurado."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    from supabase import create_client
    return create_client(url, key).storage


def _asegura_bucket(storage):
    try:
        storage.get_bucket(BUCKET)
    except Exception:
        try:
            storage.create_bucket(BUCKET, options={
                "public": True,
                "file_size_limit": MAX_BYTES,
            })
        except Exception as e:
            current_app.logger.warning("[cierre-evidencia] bucket %s: %s", BUCKET, e)


def _subir(storage, owner_kind, owner_id, archivo):
    """Sube un archivo y devuelve (url, error). Nunca lanza."""
    from werkzeug.utils import secure_filename

    if (archivo.mimetype or "") not in MIMES_PERMITIDOS:
        return None, f"«{archivo.filename}» no es un formato aceptado (PDF, imagen u Office)."
    datos = archivo.read()
    if not datos:
        return None, f"«{archivo.filename}» llegó vacío."
    if len(datos) > MAX_BYTES:
        return None, f"«{archivo.filename}» pesa más de {MAX_BYTES // (1024 * 1024)} MB."

    ruta = f"{owner_kind}/{owner_id}/{uuid.uuid4().hex}_{secure_filename(archivo.filename)}"
    try:
        bucket = storage.from_(BUCKET)
        bucket.upload(ruta, datos, file_options={"content-type": archivo.mimetype})
        return bucket.get_public_url(ruta), None
    except Exception as e:
        current_app.logger.warning("[cierre-evidencia] fallo al subir %s: %s", ruta, e)
        return None, f"No se pudo subir «{archivo.filename}». Intenta de nuevo."


def guardar(owner_kind, owner_id, tipos, files, subido_por=None):
    """Sube los archivos de cada tipo marcado y registra las evidencias.

    owner_kind: 'lead' | 'oportunidad'
    tipos:      lista de claves de TIPOS que marcó el vendedor
    files:      request.files — se leen los campos 'evidencia_<tipo>'
    Devuelve (evidencias_creadas, error). Con error, no se persiste nada:
    una evidencia a medias es peor que ninguna, porque la validación del
    cierre la contaría como suficiente.
    """
    if owner_kind not in ("lead", "oportunidad"):
        return [], "Destino de evidencia inválido."

    tipos = [t for t in dict.fromkeys(tipos or []) if t]   # únicos, en orden
    if not tipos:
        return [], "Marca al menos un respaldo de la venta."
    if len(tipos) > MAX_TIPOS:
        return [], f"Máximo {MAX_TIPOS} respaldos por cierre."
    invalidos = [t for t in tipos if t not in TIPOS]
    if invalidos:
        return [], f"Respaldo desconocido: {', '.join(invalidos)}."

    # Cada tipo marcado necesita su propio archivo. Marcar «Contrato» y no
    # subirlo deja el cierre igual de indefendible que no marcar nada.
    faltantes = [TIPOS[t] for t in tipos
                 if not [f for f in files.getlist(f"evidencia_{t}") if f and f.filename]]
    if faltantes:
        return [], "Falta el archivo de: " + ", ".join(faltantes) + "."

    storage = _get_storage()
    if not storage:
        return [], "El almacenamiento de archivos no está configurado."
    _asegura_bucket(storage)

    # El modal siempre manda el set COMPLETO de respaldos, así que esto
    # reemplaza en vez de acumular. Si acumulara, un cierre que falla y se
    # reintenta con otros dos tipos dejaría cuatro registrados y
    # validar_para_cierre rebotaría por exceso — el vendedor quedaría atorado
    # sin forma de corregirlo desde la UI.
    previas = evidencias_de(owner_kind, owner_id)

    creadas = []
    for tipo in tipos:
        archivos = [f for f in files.getlist(f"evidencia_{tipo}") if f and f.filename]
        for archivo in archivos[:MAX_ARCHIVOS_POR_TIPO]:
            url, err = _subir(storage, owner_kind, owner_id, archivo)
            if err:
                return [], err
            creadas.append(CierreEvidencia(
                lead_id=owner_id if owner_kind == "lead" else None,
                oportunidad_id=owner_id if owner_kind == "oportunidad" else None,
                tipo=tipo,
                archivo_url=url,
                archivo_nombre=archivo.filename[:300],
                subido_por=subido_por,
            ))

    # Recién aquí, con todas las subidas bien, se tira lo anterior.
    for vieja in previas:
        db.session.delete(vieja)
    for ev in creadas:
        db.session.add(ev)
    return creadas, None


def evidencias_de(owner_kind, owner_id):
    q = CierreEvidencia.query
    if owner_kind == "lead":
        q = q.filter(CierreEvidencia.lead_id == owner_id)
    else:
        q = q.filter(CierreEvidencia.oportunidad_id == owner_id)
    return q.order_by(CierreEvidencia.created_at).all()


def validar_para_cierre(owner_kind, owner_id, unidad):
    """(ok, error) — ¿se puede cerrar este trato como ganado?

    Solo bloquea a las unidades que lo exigen; el resto pasa de largo. La
    validación se hace contra lo YA guardado, no contra lo que venga en el
    request, para que no importe si el vendedor subió la evidencia en un
    paso previo o en el mismo envío.
    """
    if not requiere_evidencia(unidad):
        return True, None

    evs = evidencias_de(owner_kind, owner_id)
    if not evs:
        return False, ("Para cerrar una venta de Pestex necesitas adjuntar el "
                       "respaldo: orden de compra, contrato, correo de "
                       "confirmación, captura de WhatsApp o la cita en "
                       "Operandium / iGeo.")

    tipos = {e.tipo for e in evs}
    if len(tipos) > MAX_TIPOS:
        return False, f"Máximo {MAX_TIPOS} respaldos por cierre; hay {len(tipos)}."
    return True, None
