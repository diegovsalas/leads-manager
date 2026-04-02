# Estructura del Proyecto — Leads Manager Grupo Avantex

```
avantex_crm/
├── app.py                     # Punto de entrada: Flask + SQLAlchemy + SocketIO
├── config.py                  # Variables de entorno y configuración
├── models.py                  # Modelos SQLAlchemy (Lead, Vendedor, Mensaje, Etapa)
├── extensions.py              # Instancias compartidas (db, socketio)
│
├── blueprints/
│   ├── __init__.py
│   ├── webhooks.py            # POST /webhook/meta  y  POST /webhook/whatsapp
│   ├── leads.py               # CRUD del pipeline (mover tarjetas, crear leads)
│   └── chat.py                # Envío de mensajes por WhatsApp Cloud API
│
├── templates/
│   ├── base.html              # Layout base con Tailwind CDN + SocketIO JS
│   └── pipeline/
│       └── index.html         # Vista Kanban + Panel de Chat (todo en una página)
│
├── static/
│   ├── css/
│   │   └── custom.css         # Overrides mínimos sobre Tailwind
│   └── js/
│       ├── kanban.js          # Drag-and-drop de tarjetas
│       └── chat.js            # SocketIO client + renderizado del chat
│
├── requirements.txt
└── .env                       # Secretos (nunca al repo)
```

## requirements.txt

```
flask>=3.0
flask-sqlalchemy>=3.1
flask-socketio>=5.3
psycopg2-binary>=2.9
python-dotenv>=1.0
requests>=2.31
eventlet>=0.35          # Servidor async para SocketIO en producción
```

## .env (ejemplo — no subir a git)

```
FLASK_ENV=development
SECRET_KEY=cambia_esto_por_algo_largo_y_aleatorio
DATABASE_URL=postgresql://usuario:password@localhost:5432/avantex_crm
WHATSAPP_TOKEN=EAAxxxxxxx         # Token de acceso de Meta
WHATSAPP_PHONE_ID=1234567890      # Phone Number ID de WhatsApp Business
META_VERIFY_TOKEN=mi_token_secreto_para_verificar_webhook
```
