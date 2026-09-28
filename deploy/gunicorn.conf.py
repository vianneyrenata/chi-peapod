# ponytail: workers MUST stay 1 — the rebuild timer, REBUILD_LOCK, rate-limit windows and the
# in-memory catalogue all live in process memory; a second process would run its own unsynced copy.
# Concurrency comes from threads (threads > 1 makes gunicorn use its gthread worker). Move the
# queue/limits to the database before ever raising this.
workers = 1
threads = 32
timeout = 120  # /api/draft-bio can legitimately take ~30 s
control_socket_disable = True  # defaults to /run/user/<uid>, which the service user lacks
# No access log, by design (see the FAQ's data-collection answer); gunicorn's default is off.


def post_worker_init(worker):
    import server
    server.start_background()
