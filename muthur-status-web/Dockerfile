FROM python:3.12-slim

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin web

COPY --chown=web:web src/ /app/src/
# static/ is NOT copied in - the frontend ships via a Kubernetes ConfigMap
# mounted over /app/static (see fluxcd/muthur-status-web/), which always
# overrides whatever the image would otherwise have here.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
USER web

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1

EXPOSE 8080
ENTRYPOINT ["python", "src/app.py"]
