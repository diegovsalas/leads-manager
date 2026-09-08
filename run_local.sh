#!/usr/bin/env bash
# Levanta el CRM contra la base LOCAL, sin tocar produccion.
#
# .env apunta al Postgres de Supabase (produccion). load_dotenv() no pisa
# variables que ya estan en el entorno, asi que exportar .env.local ANTES de
# importar la app es lo que mantiene las dos separadas.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env.local ]; then
  echo "Falta .env.local — no arranco contra .env, que es PRODUCCION." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env.local
set +a

export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"

# Cinturon de seguridad: si por lo que sea DATABASE_URL quedo apuntando a
# Supabase, no arrancamos. Un boot corre migraciones automaticas.
case "${DATABASE_URL:-}" in
  *supabase*|*pooler.supabase.com*)
    echo "ABORTADO: DATABASE_URL apunta a Supabase (produccion)." >&2
    exit 1
    ;;
esac

echo "Base:      ${DATABASE_URL}"
echo "Evidencias: ${CRM_STORAGE_LOCAL_DIR:-<Supabase>}"
echo "Scheduler: ${SCHEDULER_EN_PROCESO:-1} (0 = apagado)"
echo "Abre:      http://127.0.0.1:${PORT:-5001}"
echo

exec python3 - "$@" <<'PY'
import os
from avantex_crm import create_app
from extensions import socketio

app = create_app()
socketio.run(
    app,
    host="127.0.0.1",
    port=int(os.getenv("PORT", "5001")),
    debug=True,
    # El auto-reload hace fork y gevent (que ya monkey-patcheo los hilos) no
    # sobrevive: revienta con AssertionError en un atfork callback y el
    # proceso muere. Se recarga a mano: Ctrl-C y volver a correr el script.
    use_reloader=False,
)
PY
