# Rebuttal: one process serving the seller's web app and the PayPal webhook.
#
#   docker build -t rebuttal .
#   docker run --rm -p 8080:8080 rebuttal                    # demo mode, http://localhost:8080
#   docker run --rm -p 8080:8080 --env-file .env rebuttal    # your settings, e.g. the PayPal sandbox
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY rebuttal ./rebuttal
RUN pip install .

# Runs as an ordinary user. /data holds the SQLite files; set SNAPSHOT_DIR to a mounted
# bucket to keep them across restarts.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
ENV DATA_DIR=/data PORT=8080

EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn rebuttal.web:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
