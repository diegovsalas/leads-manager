# extensions.py
# Instancias compartidas para evitar importaciones circulares

from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO

db = SQLAlchemy()
socketio = SocketIO()
