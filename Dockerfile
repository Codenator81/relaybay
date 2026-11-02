# relaybay server image. Publish the port only on the host's loopback:
#   docker run -d --name relaybay -p 127.0.0.1:8765:8765 -v relaybay-data:/data ghcr.io/codenator81/relaybay
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="relaybay" \
      org.opencontainers.image.description="A live board and MCP server for coordinating many Claude Code sessions across projects." \
      org.opencontainers.image.source="https://github.com/Codenator81/relaybay" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAYBAY_HOME=/data \
    RELAYBAY_HOST=0.0.0.0 \
    RELAYBAY_PORT=8765
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels \
    && useradd --system --uid 10001 --user-group --home-dir /data --no-create-home relaybay \
    && mkdir /data && chown relaybay:relaybay /data
USER relaybay
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; import os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('RELAYBAY_PORT', '8765'), timeout=2)"]
CMD ["relaybay", "serve"]
