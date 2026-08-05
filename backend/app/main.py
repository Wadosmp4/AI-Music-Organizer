from fastapi import FastAPI

from app.api.v1 import router as api_v1_router

app = FastAPI(title="AI Music Organizer")

app.include_router(api_v1_router)
