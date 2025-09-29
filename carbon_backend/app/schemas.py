from pydantic import BaseModel
from typing import Optional

class ProjectBase(BaseModel):
    name: str

class ProjectCreate(ProjectBase):
    pass

class Project(ProjectBase):
    id: int
    status: str
    total_co2_tonnes: Optional[float] = None

    class Config:
        orm_mode = True