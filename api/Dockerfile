# Build context is the REPO ROOT, not api/ — the image needs both api/ (the
# FastAPI app) and agent/ (the tools the app will call). See the agent-api
# service in docker-compose.yml, and .dockerignore for what is excluded.
FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt ./requirements.txt
COPY agent/requirements.txt ./requirements-agent.txt
RUN pip install --no-cache-dir -r requirements.txt -r requirements-agent.txt

COPY api/app ./app
COPY agent ./agent

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
