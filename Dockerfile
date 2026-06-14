FROM python:3.11-slim

RUN useradd -m driftctl
WORKDIR /app

COPY . .
RUN pip install --no-cache-dir .

USER driftctl
EXPOSE 8080

ENTRYPOINT ["driftctl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
