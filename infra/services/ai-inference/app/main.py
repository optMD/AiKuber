import hashlib
import json
import os
import time
import urllib.error
import urllib.request
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
CLAUDEHUB_BASE_URL = os.environ.get("CLAUDEHUB_BASE_URL", "https://api.claudehub.fun")
CLAUDEHUB_API_KEY = os.environ.get("CLAUDEHUB_API_KEY", "")
CLAUDEHUB_MODEL = os.environ.get("CLAUDEHUB_MODEL", "claude-haiku-4.5")
CLAUDEHUB_MAX_TOKENS = int(os.environ.get("CLAUDEHUB_MAX_TOKENS", "1024"))
CLAUDEHUB_TIMEOUT_SECONDS = int(os.environ.get("CLAUDEHUB_TIMEOUT_SECONDS", "60"))
UPSTREAM_MAX_RETRIES = 3
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

REQUESTS = Counter("ai_requests", "Запросы к /api/ask по коду ответа", ["status"])
LATENCY = Histogram("ai_request_latency_seconds", "Время обработки запроса к /api/ask")
CACHE_REQUESTS = Counter("ai_cache_requests", "Результаты обращений к Redis-кэшу", ["result"])
UPSTREAM_REQUESTS = Counter("ai_upstream_requests", "Запросы к внешнему API", ["status"])
UPSTREAM_LATENCY = Histogram("ai_upstream_latency_seconds", "Время ответа внешнего API")
UPSTREAM_RETRIES = Counter("ai_upstream_retries", "Повторные попытки обращения к внешнему API")
TOKEN_USAGE = Counter("ai_token_usage", "Количество токенов внешнего API", ["direction"])

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


class UpstreamError(Exception):
    def __init__(self, status_code: int):
        super().__init__(status_code)
        self.status_code = status_code


def call_claudehub(prompt: str) -> str:
    payload = json.dumps(
        {
            "model": CLAUDEHUB_MODEL,
            "max_tokens": CLAUDEHUB_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        CLAUDEHUB_BASE_URL + "/v1/messages",
        data=payload,
        method="POST",
        headers={
            "x-api-key": CLAUDEHUB_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    for attempt in range(UPSTREAM_MAX_RETRIES + 1):
        attempt_start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=CLAUDEHUB_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read())
            UPSTREAM_REQUESTS.labels(status=str(resp.status)).inc()
            UPSTREAM_LATENCY.observe(time.perf_counter() - attempt_start)
            usage = data.get("usage", {})
            for direction, key in (("input", "input_tokens"), ("output", "output_tokens")):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    TOKEN_USAGE.labels(direction=direction).inc(value)
            text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
            if not text:
                raise UpstreamError(502)
            return text
        except urllib.error.HTTPError as e:
            UPSTREAM_REQUESTS.labels(status=str(e.code)).inc()
            UPSTREAM_LATENCY.observe(time.perf_counter() - attempt_start)
            if e.code not in RETRYABLE_STATUSES or attempt == UPSTREAM_MAX_RETRIES:
                raise UpstreamError(e.code)
            UPSTREAM_RETRIES.inc()
            time.sleep(float(e.headers.get("Retry-After", "1")) if e.code == 429 else 2**attempt)
        except (urllib.error.URLError, TimeoutError):
            UPSTREAM_REQUESTS.labels(status="network_error").inc()
            UPSTREAM_LATENCY.observe(time.perf_counter() - attempt_start)
            if attempt == UPSTREAM_MAX_RETRIES:
                raise UpstreamError(502)
            UPSTREAM_RETRIES.inc()
            time.sleep(2**attempt)


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
            CACHE_REQUESTS.labels(result="hit").inc()
        else:
            CACHE_REQUESTS.labels(result="miss").inc()
            try:
                answer = call_claudehub(body.prompt)
            except UpstreamError as e:
                status = 502
                raise HTTPException(status_code=502, detail=f"Внешний API вернул ошибку {e.status_code}") from e
            from_cache = False
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
