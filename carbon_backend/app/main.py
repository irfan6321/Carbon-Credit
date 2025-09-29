from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from . import models, schemas, database, tasks
import os

# Create database tables on startup
models.Base.metadata.create_all(bind=database.engine)

app = FastAPI()

# Dependency to get a DB session
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/projects/", response_model=schemas.Project)
def create_project(project: schemas.ProjectCreate, db: Session = Depends(get_db)):
    """
    Create a new project and start the processing pipeline.
    This simulates uploading images by using the sample data.
    """
    # Create a directory for this project's data
    project_data_path = os.path.join(os.getenv("DATA_DIRECTORY"), str(project.name))
    os.makedirs(project_data_path, exist_ok=True)

    db_project = models.Project(name=project.name, status="ACCEPTED")
    db.add(db_project)
    db.commit()
    db.refresh(db_project)

    # Start the background processing task
    tasks.start_processing_pipeline.delay(db_project.id)

    return db_project

@app.get("/projects/{project_id}", response_model=schemas.Project)
def get_project_status(project_id: int, db: Session = Depends(get_db)):
    """
    Get the status and results of a specific project.
    """
    db_project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return db_project