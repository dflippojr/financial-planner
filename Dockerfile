FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
RUN chmod +x /app/scripts/start-production.sh

USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/health/', headers={'X-Forwarded-Proto':'https'}); urllib.request.urlopen(request, timeout=3)"]

ENTRYPOINT ["/app/scripts/start-production.sh"]
