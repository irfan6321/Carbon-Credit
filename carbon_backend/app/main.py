# app/main.py
from fastapi import FastAPI, Depends, HTTPException, File, UploadFile
from sqlalchemy.orm import Session
from typing import List
import shutil
import os
from . import models, schemas, database, tasks

# This line is no longer needed here as db_init handles it
# models.Base.metadata.create_all(bind=database.engine)

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
    Creates a new project record in the database and prepares folders for image uploads.
    """
    # Create a project record first to get an ID
    clean_name = project.name.strip()
    db_project = models.Project(name=clean_name, status="PENDING_UPLOAD")
    db.add(db_project)
    db.commit()
    db.refresh(db_project)

    # Now create directories using the confirmed project ID
    project_data_path = os.path.join(os.getenv("DATA_DIRECTORY"), str(db_project.id))
    os.makedirs(project_data_path, exist_ok=True)
    os.makedirs(os.path.join(project_data_path, "raw_images"), exist_ok=True)

    return db_project

@app.post("/projects/{project_id}/upload-images/")
def upload_project_images(project_id: int, files: List[UploadFile] = File(...), db: Session = Depends(get_db)):
    """
    Receives image files for a project, saves them, and starts the processing pipeline.
    """
    db_project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_images_path = os.path.join(os.getenv("DATA_DIRECTORY"), str(db_project.id), "raw_images")

    # Save all uploaded files to the project's folder
    for file in files:
        file_location = os.path.join(project_images_path, file.filename)
        with open(file_location, "wb+") as file_object:
            shutil.copyfileobj(file.file, file_object)

    # Update status and start the background processing task
    db_project.status = "ACCEPTED"
    db.commit()

    # This is where the magic happens: we kick off the Celery task
    tasks.start_processing_pipeline.delay(project_id)

    return {"message": f"Successfully uploaded {len(files)} files. Processing started for project ID {project_id}."}

@app.get("/projects/{project_id}", response_model=schemas.Project)
def get_project_status(project_id: int, db: Session = Depends(get_db)):
    """
    Gets the current status and results of a project.
    """
    db_project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return db_project