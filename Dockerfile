FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements-api.txt .
RUN pip install -r requirements-api.txt
COPY backend ./backend
RUN useradd -m app && chown -R app /app
USER app
# Cloud Run inyecta $PORT (8080 por defecto). Un solo proceso: los trabajos viven en memoria (docs/RIESGOS_API.md)
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75"]
