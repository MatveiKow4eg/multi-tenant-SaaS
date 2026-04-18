from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.auth import router as auth_router
from app.api.routes.members import router as members_router
from app.api.routes.operations import router as operations_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.companies import router as companies_router
from app.api.routes.domains import router as domains_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["health"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(members_router, prefix="/members", tags=["members"])
api_router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
api_router.include_router(companies_router, prefix="/companies", tags=["companies"])
api_router.include_router(operations_router, prefix="/operations", tags=["operations"])
api_router.include_router(domains_router, prefix="/sender-domains", tags=["sender-domains"])
api_router.include_router(domains_router, prefix="/domains", tags=["domains"])
