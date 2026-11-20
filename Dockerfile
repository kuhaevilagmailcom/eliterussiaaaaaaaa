FROM python:3.12.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system mgn && adduser --system --ingroup mgn --home /app mgn \
    && mkdir -p /app/data /app/subscription_cache \
    && chown -R mgn:mgn /app

COPY --chown=mgn:mgn . .

# BotHost builds from a Windows-backed checkout where directory execute bits
# can be lost. aiohttp must be able to traverse the bundled Mini App assets.
RUN chmod -R u=rwX,g=rX,o=rX /app

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/miniapp/health', timeout=3)" || exit 1

CMD ["python", "docker_entrypoint.py"]
