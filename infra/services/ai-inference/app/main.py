import hashlib
import os
import time
from contextlib import asynccontextmanager

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "ai")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "ai")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))

STUB_ANSWER = "Заглушка: внешний API ещё не подключён. Запрос получен и сохранён в истории."

REQUESTS = Counter("ai_requests", "Запросы к /api/ask по коду ответа", ["status"])
LATENCY = Histogram("ai_request_latency_seconds", "Время обработки запроса к /api/ask")

redis_client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


def db_connect():
    return psycopg.connect(
        host=POSTGRES_HOST,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def init_db():
    for _ in range(30):
        try:
            with db_connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS requests (
                        id SERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        prompt TEXT NOT NULL,
                        answer TEXT NOT NULL,
                        from_cache BOOLEAN NOT NULL
                    )
                    """
                )
            return
        except psycopg.OperationalError:
            time.sleep(2)
    raise RuntimeError("PostgreSQL недоступен")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


class AskIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/ask")
def ask(body: AskIn, request: Request):
    start = time.perf_counter()
    status = 500
    try:
        client_ip = request.headers.get("x-real-ip", request.client.host)
        rate_key = f"rate:{client_ip}:{int(time.time() // 60)}"
        count = redis_client.incr(rate_key)
        if count == 1:
            redis_client.expire(rate_key, 60)
        if count > RATE_LIMIT_PER_MINUTE:
            status = 429
            raise HTTPException(status_code=429, detail="Превышен лимит запросов в минуту")

        cache_key = "answer:" + hashlib.sha256(body.prompt.encode()).hexdigest()
        cached = redis_client.get(cache_key)
        if cached is not None:
            answer, from_cache = cached, True
        else:
            answer, from_cache = STUB_ANSWER, False
            redis_client.set(cache_key, answer, ex=CACHE_TTL_SECONDS)

        with db_connect() as conn:
            conn.execute(
                "INSERT INTO requests (prompt, answer, from_cache) VALUES (%s, %s, %s)",
                (body.prompt, answer, from_cache),
            )

        status = 200
        return {"answer": answer, "from_cache": from_cache}
    finally:
        REQUESTS.labels(status=str(status)).inc()
        LATENCY.observe(time.perf_counter() - start)


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
