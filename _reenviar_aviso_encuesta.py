"""Reenvía el aviso de respuestas de encuesta que entraron antes de que
existiera la notificación (FEAT-2026-10-05).

Solo lo manda a quien se indique en --para; NO le vuelve a escribir al KAM.
Sin --enviar solo muestra lo que encontró. La base la decide DATABASE_URL.

    python3 _reenviar_aviso_encuesta.py --fecha 2026-10-05 \\
        --cliente "Innova Sport" --cliente "Farmacias del Ahorro" \\
        --para jessicasantin@grupoavantex.com            # revisar
    ... --enviar                                         # mandar
"""
import argparse
from datetime import date

BASE_URL = "https://leads-manager-avantex.onrender.com"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fecha", required=True, help="día de la respuesta, AAAA-MM-DD (hora de Monterrey)")
    ap.add_argument("--cliente", action="append", required=True)
    ap.add_argument("--para", action="append", required=True)
    ap.add_argument("--enviar", action="store_true")
    args = ap.parse_args()
    dia = date.fromisoformat(args.fecha)

    from avantex_crm import create_app
    from extensions import db
    from models import CSAccount, CSEncuesta
    from blueprints import encuesta

    app = create_app()
    with app.app_context(), app.test_request_context(base_url=BASE_URL):
        filas = (db.session.query(CSEncuesta, CSAccount)
                 .join(CSAccount, CSEncuesta.account_id == CSAccount.id)
                 .filter(CSAccount.nombre.in_(args.cliente))
                 .filter(db.func.date(db.func.timezone("America/Monterrey", CSEncuesta.created_at)) == dia)
                 .order_by(CSEncuesta.created_at).all())

        encontrados = {a.nombre for _, a in filas}
        for falta in sorted(set(args.cliente) - encontrados):
            print(f"  ! sin respuesta ese día: {falta}")
        for e, a in filas:
            print(f"  {a.nombre} · {e.nombre_respondente} · NPS {e.nps} · CSAT {e.csat_promedio}")
            if args.enviar:
                ok = encuesta._notificar_respuesta(a, e, para=args.para, cc=[])
                print("    enviado" if ok else "    NO se envió (revisa RESEND_API_KEY / logs)")
        if not args.enviar:
            print(f"\n{len(filas)} respuesta(s). Nada enviado: agrega --enviar para mandarlas a {', '.join(args.para)}.")


if __name__ == "__main__":
    main()
