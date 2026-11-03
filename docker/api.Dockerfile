FROM python:3.11-slim

WORKDIR /app

# DejaVu Sans for the PDF reports (Polish letters).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api /app/api
COPY data /app/data
COPY docs/MART.md docs/DEFINITIONS.md /app/docs/
# Rebuilds the OpenSearch question log from Postgres (docs/DEPLOYMENT.md, Backups).
COPY scripts/reindex_question_log.py /app/scripts/

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
