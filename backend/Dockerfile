FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system gpuops \
    && adduser --system --ingroup gpuops gpuops \
    && mkdir -p /app/data \
    && chown -R gpuops:gpuops /app/data

COPY --chown=gpuops:gpuops app ./app

USER gpuops

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
