FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend/server.py /app/server.py
CMD ["sh","-c","gunicorn --workers 1 --threads 2 --timeout 300 --bind 0.0.0.0:${PORT:-10000} server:app"]
