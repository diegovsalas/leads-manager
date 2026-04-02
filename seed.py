# seed.py
"""
Ejecutar una vez para insertar el equipo comercial inicial.
Uso: python seed.py
"""
from avantex_crm import create_app
from extensions import db
from models import Usuario, RolComercial


VENDEDORES = [
    {
        "nombre": "Azael Olivo",
        "telefono_whatsapp": "",  # Llenar con numero real
        "rol_comercial": RolComercial.ASESOR_COMERCIAL,
        "especialidad_marca": ["Weldex", "Aromatex", "Nexo"],
    },
    {
        "nombre": "Janeth Sauceda",
        "telefono_whatsapp": "",
        "rol_comercial": RolComercial.ASESOR_COMERCIAL,
        "especialidad_marca": ["Aromatex Home", "Pestex", "Nexo"],
    },
    {
        "nombre": "Damariz Romero",
        "telefono_whatsapp": "",
        "rol_comercial": RolComercial.ASESOR_COMERCIAL,
        "especialidad_marca": ["Pestex", "Aromatex Home", "Weldex"],
    },
]


def seed():
    app = create_app()
    with app.app_context():
        for data in VENDEDORES:
            existe = Usuario.query.filter_by(nombre=data["nombre"]).first()
            if existe:
                print(f"  Ya existe: {data['nombre']}")
                continue
            usuario = Usuario(**data)
            db.session.add(usuario)
            print(f"  Creado: {data['nombre']} — {data['especialidad_marca']}")

        db.session.commit()
        print("\nEquipo comercial listo:")
        for u in Usuario.query.all():
            print(f"  {u.nombre} | {u.especialidad_marca} | en_turno={u.en_turno}")


if __name__ == "__main__":
    seed()
