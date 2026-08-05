from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from database import engine, Base
from routers import ingest, auth, projects, dashboard, alerts, billing


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="PromptWatch API",
    description="LLM cost tracking and observability",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(ingest.router, prefix="/v1", tags=["ingest"])
app.include_router(projects.router, prefix="/projects", tags=["projects"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
app.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
app.include_router(billing.router, prefix="/billing", tags=["billing"])


@app.get("/health")
async def health():
    return {"status": "ok"}
