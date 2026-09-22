FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 依存だけ先に入れてレイヤキャッシュを効かせる
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# SQLite の置き場。compose でホストにマウントする。
RUN mkdir -p /app/data && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV DATABASE_URL=sqlite:////app/data/app.db
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
