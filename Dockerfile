# The live-demo image: ONE process runs the API, the dispatcher, the event publisher and the
# workers (EMBEDDED_WORKERS, switched on by LIVE_DEMO). One process is the point: a free host
# gives you one service, and the chaos buttons act on the workers living inside it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# Dependencies first, so editing code does not reinstall them (layer cache). They are read from
# pyproject.toml so there is one list; requirements.lock pins every version that was tested
# (a new redis-py once changed a default and broke the workers); the app itself is run from
# /srv, not pip-installed. Regenerate the lock: see docs/DEPLOY.md.
COPY pyproject.toml requirements.lock ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
 && pip install -r requirements.txt -c requirements.lock

COPY app ./app
COPY migrations ./migrations

# Do not run as root.
RUN useradd --create-home --uid 10001 app && chown -R app /srv
USER app

# Render sets PORT. Never more than one uvicorn worker: workers and chaos state live in-process.
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
