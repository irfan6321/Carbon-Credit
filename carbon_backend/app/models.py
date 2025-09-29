from sqlalchemy import Column, Integer, String, Float
from .database import Base

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    status = Column(String, default="PENDING")
    chm_path = Column(String, nullable=True)
    crowns_path = Column(String, nullable=True)
    carbon_results_path = Column(String, nullable=True)
    total_co2_tonnes = Column(Float, nullable=True)