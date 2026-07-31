# Playwright's own image already contains Chromium and every system library it
# needs, which is the hard part of containerising a browser runner.
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QA_WORKSPACE=/data \
    QA_DB_PATH=/data/qa_platform.db \
    QA_SCHEDULER=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium

COPY app ./app
COPY web ./web
COPY qa_agent.py ./
COPY tests ./tests
COPY samples ./samples

# Test data, reports and evidence live on a volume so containers stay disposable.
RUN mkdir -p /data/projects
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
