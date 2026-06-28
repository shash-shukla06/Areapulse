# gunicorn.conf.py
bind = "0.0.0.0:8000"
workers = 2
timeout = 120
keepalive = 5

def post_fork(server, worker):
    from database import _state, init_db
    if _state.get('pg_pool'):
        try:
            _state['pg_pool'].close()
        except Exception:
            pass
        _state['pg_pool'] = None
        _state['mode'] = 'memory'
    init_db()
