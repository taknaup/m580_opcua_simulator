FROM python:3.12.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --system --uid 10001 sim && mkdir -p /data && chown sim /data
USER sim

ENV DATA_DIR=/data \
    OPCUA_PORT=4840 \
    WEB_PORT=8000
EXPOSE 4840 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ['WEB_PORT'], timeout=4)"

CMD ["python", "-m", "app.main"]
