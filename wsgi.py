from app import create_app
from services.realtime import socketio
application = create_app()
_ = socketio
