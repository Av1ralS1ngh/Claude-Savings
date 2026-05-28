FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DECOMPUTE_HOST=0.0.0.0 \
    DECOMPUTE_PORT=8080 \
    DECOMPUTE_DATA_DIR=/data \
    DECOMPUTE_SQLITE_PATH=/data/decompute.db
COPY apps/api/requirements.txt /app/apps/api/requirements.txt
RUN pip install --no-cache-dir -r /app/apps/api/requirements.txt
COPY apps /app/apps
COPY packages /app/packages
RUN pip install --no-cache-dir -e /app/packages/python-sdk
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "decompute_gateway.main:app", "--app-dir", "apps/api", "--host", "0.0.0.0", "--port", "8080"]
