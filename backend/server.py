import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from lib.db import client, ensure_indexes  # noqa: E402
from lib.middleware import RequestAuditMiddleware  # noqa: E402
from routers import assistant, attendance, auth, company, dashboard, inventory, job_cards, machines, orders, production, reports, schedule, session  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ENABLE_DEMO_SEED = os.environ.get("ENABLE_DEMO_SEED", "false").strip().lower() == "true"
CORS_ORIGINS = [origin.strip() for origin in os.environ.get(
    "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if origin.strip()]
if not CORS_ORIGINS or "*" in CORS_ORIGINS:
    raise RuntimeError("CORS_ORIGINS must contain explicit trusted origins when credentials are enabled")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.index_task = asyncio.create_task(ensure_indexes())
    yield
    client.close()


app = FastAPI(title="FactoryOS API", lifespan=lifespan)

api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"message": "FactoryOS API", "demo_mode": ENABLE_DEMO_SEED}


for r in (auth, company, session, orders, job_cards, attendance, production, machines, inventory, schedule, assistant, dashboard, reports):
    api_router.include_router(r.router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestAuditMiddleware)

# Include the router in the main app (must stay last)
app.include_router(api_router)
