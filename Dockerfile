# One file, one process, no access log. The access log is off on purpose:
# it is the only place a sender's IP address would ever be written.
FROM python:3.12-alpine

RUN adduser -D -u 1000 pulse && mkdir -p /data && chown pulse /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY pulse ./pulse
RUN pip install --no-cache-dir . && rm -rf /root/.cache

USER pulse
VOLUME ["/data"]
EXPOSE 8750
HEALTHCHECK --interval=60s --timeout=3s CMD wget -qO- http://127.0.0.1:8750/healthz || exit 1
CMD ["uvicorn", "pulse.app:app", "--host", "0.0.0.0", "--port", "8750", "--no-access-log", "--proxy-headers"]
