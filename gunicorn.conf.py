import os, threading

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
worker_class = "gthread"
threads = 4
timeout = 300
graceful_timeout = 60
keepalive = 5
preload_app = False  # import app fresh in each worker, so threads survive fork

def post_fork(server, worker):
    """Called in the worker process after fork — safe to start threads here."""
    from app import _init_og, _ping
    import threading
    threading.Thread(target=_init_og, daemon=True).start()
    threading.Thread(target=_ping, daemon=True).start()
