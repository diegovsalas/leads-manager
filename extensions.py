# extensions.py
# Instancias compartidas para evitar importaciones circulares

from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
socketio = SocketIO()
# Almacenamiento en memoria: suficiente para el único worker gevent actual
# (gunicorn -w 1). Si se escala a más workers, cambiar a un storage_uri con Redis.
limiter = Limiter(key_func=get_remote_address)
