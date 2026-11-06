"""Punto de entrada: `python -m uvicorn backend.main:app --port 8000`.

Toda la lógica HTTP vive en `backend/api/`. Variables de entorno: ver `backend/api/settings.py` y `docs/API_DESPLIEGUE.md`.
"""
from backend.api.app import create_app

app = create_app()
