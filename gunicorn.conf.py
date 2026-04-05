import os
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
worker_class = "gevent"
worker_connections = 10
timeout = 300
graceful_timeout = 60
keepalive = 5
