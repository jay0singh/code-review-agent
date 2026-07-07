FROM python:3.13-slim

# Flush log output immediately so `docker compose logs` shows it live.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# Hosts like Render inject PORT; default stays 8001 for local/compose.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
