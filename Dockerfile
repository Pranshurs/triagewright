FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[server,mcp]" \
 && useradd --create-home --uid 10001 triagewright \
 && mkdir -p /data/runs && chown -R triagewright /data

USER triagewright
ENV TRIAGEWRIGHT_RUNS=/data/runs
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/scenarios', timeout=2)"
CMD ["triagewright", "serve", "--host", "0.0.0.0", "--port", "8000", "--runs", "/data/runs"]
