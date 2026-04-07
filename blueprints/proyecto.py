# blueprints/proyecto.py
import os
import requests
from flask import Blueprint, request, jsonify
from extensions import db
from models import ProyectoItem

proyecto_bp = Blueprint("proyecto", __name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)


def _generar_prompt_dev(titulo, descripcion):
    """Llama a Gemini para generar un prompt de desarrollo para la idea."""
    if not GEMINI_API_KEY:
        return None

    prompt_texto = (
        f"Eres un arquitecto de software experto. Genera un prompt detallado "
        f"(en español) para un asistente de IA de programación (como Claude) "
        f"que implemente la siguiente idea dentro de un CRM construido con "
        f"Flask, Supabase (PostgreSQL), WhatsApp Cloud API, Meta Ads API y "
        f"un pipeline Kanban.\n\n"
        f"Idea: {titulo}\n"
        f"Descripción: {descripcion or 'Sin descripción adicional'}\n\n"
        f"El prompt debe incluir:\n"
        f"- Contexto del sistema existente\n"
        f"- Archivos a crear o modificar\n"
        f"- Modelos de datos necesarios\n"
        f"- Endpoints API\n"
        f"- Lógica de negocio paso a paso\n"
        f"- Consideraciones de seguridad y edge cases\n"
    )

    try:
        resp = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt_texto}]}]
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return None


@proyecto_bp.route("/items", methods=["GET"])
def listar_items():
    """Lista items del proyecto, opcionalmente filtrados por tipo."""
    tipo = request.args.get("tipo")
    query = ProyectoItem.query.filter(ProyectoItem.parent_id.is_(None))
    query = query.order_by(ProyectoItem.fecha_creacion.desc())
    if tipo:
        query = query.filter_by(tipo=tipo)
    items = query.all()
    return jsonify([i.to_dict() for i in items])


@proyecto_bp.route("/items", methods=["POST"])
def crear_item():
    """Crea un item de proyecto. Si es idea, genera prompt_dev con Gemini."""
    data = request.get_json() or {}

    tipo = data.get("tipo")
    titulo = data.get("titulo")
    autor = data.get("autor")

    if not tipo or not titulo or not autor:
        return jsonify({"error": "tipo, titulo y autor son requeridos"}), 400

    if tipo not in ("avance", "idea", "nota", "subtarea"):
        return jsonify({"error": "tipo debe ser avance, idea, nota o subtarea"}), 400

    descripcion = data.get("descripcion")
    prioridad = data.get("prioridad")
    parent_id = data.get("parent_id")
    fase_num = data.get("fase_num")
    prompt_dev = None

    if tipo == "idea":
        prompt_dev = _generar_prompt_dev(titulo, descripcion)

    item = ProyectoItem(
        tipo=tipo,
        titulo=titulo,
        descripcion=descripcion,
        autor=autor,
        prioridad=prioridad,
        prompt_dev=prompt_dev,
        parent_id=parent_id,
        fase_num=fase_num,
    )
    db.session.add(item)
    db.session.commit()

    return jsonify(item.to_dict()), 201


@proyecto_bp.route("/items/<uuid:item_id>", methods=["PUT"])
def actualizar_item(item_id):
    """Actualiza un item del proyecto."""
    item = db.session.get(ProyectoItem, item_id)
    if not item:
        return jsonify({"error": "Item no encontrado"}), 404

    data = request.get_json() or {}
    if "titulo" in data:
        item.titulo = data["titulo"]
    if "descripcion" in data:
        item.descripcion = data["descripcion"]
    if "prioridad" in data:
        item.prioridad = data["prioridad"]
    if "completado" in data:
        item.completado = data["completado"]
    if "fase_num" in data:
        item.fase_num = data["fase_num"]

    db.session.commit()
    return jsonify(item.to_dict())


@proyecto_bp.route("/items/<uuid:item_id>/toggle", methods=["POST"])
def toggle_completado(item_id):
    """Toggle completado de un item."""
    item = db.session.get(ProyectoItem, item_id)
    if not item:
        return jsonify({"error": "Item no encontrado"}), 404

    item.completado = not item.completado
    db.session.commit()
    return jsonify(item.to_dict())


@proyecto_bp.route("/items/<uuid:item_id>/generar-prompt", methods=["POST"])
def generar_prompt(item_id):
    """Genera o regenera el prompt de desarrollo con Gemini."""
    item = db.session.get(ProyectoItem, item_id)
    if not item:
        return jsonify({"error": "Item no encontrado"}), 404

    prompt_dev = _generar_prompt_dev(item.titulo, item.descripcion)
    if prompt_dev:
        item.prompt_dev = prompt_dev
        db.session.commit()
        return jsonify(item.to_dict())
    else:
        return jsonify({"error": "No se pudo generar el prompt. Verifica la API key de Gemini."}), 500


@proyecto_bp.route("/items/<uuid:item_id>/votar", methods=["POST"])
def votar_item(item_id):
    """Incrementa los votos de un item en 1."""
    item = db.session.get(ProyectoItem, item_id)
    if not item:
        return jsonify({"error": "Item no encontrado"}), 404

    item.votos = (item.votos or 0) + 1
    db.session.commit()

    return jsonify(item.to_dict())


@proyecto_bp.route("/items/<uuid:item_id>", methods=["DELETE"])
def eliminar_item(item_id):
    """Elimina un item del proyecto."""
    item = db.session.get(ProyectoItem, item_id)
    if not item:
        return jsonify({"error": "Item no encontrado"}), 404

    db.session.delete(item)
    db.session.commit()

    return jsonify({"ok": True, "id": str(item_id)})
