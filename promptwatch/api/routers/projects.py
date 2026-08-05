import secrets
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from database import get_db
from models import Project, Organization, User, LLMEvent, PLAN_LIMITS
from routers.auth import get_current_user


router = APIRouter()


class CreateProjectRequest(BaseModel):
    name: str


@router.get("/")
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.org_id == current_user.org_id)
    )
    projects = result.scalars().all()
    return [{"id": p.id, "name": p.name, "api_key": p.api_key, "created_at": p.created_at} for p in projects]


@router.post("/")
async def create_project(
    req: CreateProjectRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    org_result = await db.execute(select(Organization).where(Organization.id == current_user.org_id))
    org = org_result.scalar_one()

    limits = PLAN_LIMITS[org.plan]
    if limits["projects"] != -1:
        count_result = await db.execute(
            select(func.count()).select_from(Project).where(Project.org_id == org.id)
        )
        count = count_result.scalar()
        if count >= limits["projects"]:
            raise HTTPException(status_code=402, detail=f"Plan limit: upgrade to create more projects")

    project = Project(
        id=str(uuid.uuid4()),
        org_id=org.id,
        name=req.name,
        api_key="pw_" + secrets.token_urlsafe(32),
    )
    db.add(project)
    await db.flush()
    return {"id": project.id, "name": project.name, "api_key": project.api_key}


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.org_id == current_user.org_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.delete(project)
    return {"ok": True}


@router.post("/{project_id}/rotate-key")
async def rotate_api_key(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.org_id == current_user.org_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.api_key = "pw_" + secrets.token_urlsafe(32)
    return {"api_key": project.api_key}
