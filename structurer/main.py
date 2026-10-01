"""FastAPI entrypoint:  uvicorn structurer.main:app --host 127.0.0.1 --port 8000 --workers 1"""
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from . import config, pipeline, store
from .logging_setup import log
from .ollama_pool import keys
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    store.recover()
    app.state.client = httpx.AsyncClient(timeout=config.HTTP_TIMEOUT)
    tasks = pipeline.start_background()
    log.info("started: %d workers, %d Ollama keys, model=%s, duplicate-check=%s",
             config.WORKERS, len(keys), config.OLLAMA_MODEL,
             "api+cache" if config.DUPLICATE_CHECK_API_URL else "cache-only")
    yield
    for t in tasks:
        t.cancel()
    await app.state.client.aclose()


app = FastAPI(title="ATS Structurer", lifespan=lifespan)
app.include_router(router)
