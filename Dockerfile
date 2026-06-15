FROM python:3.11-slim

RUN useradd -m driftctl
WORKDIR /app

COPY . .
RUN pip install --no-cache-dir . psycopg2-binary

RUN mkdir -p /data && chown driftctl:driftctl /data

USER driftctl
EXPOSE 8080

ENV DRIFTCTL_DB=/data/driftctl.db

ENTRYPOINT ["driftctl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
