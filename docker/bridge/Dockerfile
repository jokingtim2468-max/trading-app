# Bridge (FastAPI) + built React UI in one small, read-only, non-root container.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BRIDGE_HOST=0.0.0.0 BRIDGE_PORT=8765
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 bridge
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=ui /ui/dist /app/frontend/dist
USER bridge
EXPOSE 8765
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s CMD curl -fsS http://127.0.0.1:8765/health || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
