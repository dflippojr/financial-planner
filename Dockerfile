FROM debian:bookworm-slim AS css

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY scripts/css_pins.env scripts/css_pins.env
COPY static/src static/src
COPY static/js static/js
COPY templates templates

ARG TARGETARCH=amd64
RUN set -eu; \
    . scripts/css_pins.env; \
    echo "$DAISYUI_SHA256  static/src/vendor/daisyui.mjs" | sha256sum -c -; \
    echo "$DAISYUI_THEME_SHA256  static/src/vendor/daisyui-theme.mjs" | sha256sum -c -; \
    case "$TARGETARCH" in \
      amd64) asset=tailwindcss-linux-x64; expected="$TAILWIND_SHA256_LINUX_X64" ;; \
      arm64) asset=tailwindcss-linux-arm64; expected="$TAILWIND_SHA256_LINUX_ARM64" ;; \
      *) echo "Unsupported TARGETARCH=$TARGETARCH" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"; \
    curl -fsSL --retry 3 -o /tmp/tailwindcss "$url"; \
    echo "$expected  /tmp/tailwindcss" | sha256sum -c -; \
    chmod +x /tmp/tailwindcss; \
    mkdir -p static/dist; \
    /tmp/tailwindcss -i static/src/app.css -o static/dist/app.css --minify

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
COPY --from=css --chown=app:app /src/static/dist/app.css /app/static/dist/app.css
RUN chmod +x /app/scripts/start-production.sh /app/scripts/build_css.sh /app/scripts/run-background.sh

RUN mkdir -p /receipts && chown app:app /receipts

RUN DJANGO_SECRET_KEY=build-collectstatic-only \
    DJANGO_SECURE_SSL_REDIRECT=false \
    DJANGO_SECURE_COOKIES=false \
    python manage.py collectstatic --noinput --ignore src --ignore *.mjs --ignore SHA256SUMS \
    && chown -R app:app /app/staticfiles

USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "financial_planner.healthcheck"]

ENTRYPOINT ["/app/scripts/start-production.sh"]
