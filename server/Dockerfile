FROM python:3.13-slim
EXPOSE 8000

WORKDIR /app

RUN pip install uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . /app/

ENV KOBO_DB_PATH=/data/server.db
VOLUME ["/data"]

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
