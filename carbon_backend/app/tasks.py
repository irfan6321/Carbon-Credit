# app/tasks.py
import os
import shutil
from celery import Celery
from sqlalchemy.orm import Session
from osgeo import gdal
import rasterio
from rasterio import features, mask
import numpy as np
from scipy import ndimage as ndi
from skimage.segmentation import watershed,_watershed
from skimage.feature import peak_local_max
from skimage.transform import rescale
import geopandas as gpd
import pandas as pd
from shapely.geometry import shape
from . import database, models
import time
import requests
import zipfile
import io

# --- Celery Configuration ---
celery_app = Celery(
    "tasks",
    broker="redis://redis:6379/0",
    backend="redis://redis:6379/0"
)
celery_app.conf.update(task_track_started=True)

# --- Helper Functions ---
def get_db():
    return database.SessionLocal()

def update_project_status(db: Session, project_id: int, status: str, data: dict = None):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project:
        project.status = status
        if data:
            for key, value in data.items():
                setattr(project, key, value)
        db.commit()

# --- Placeholder Scientific & Filter Coefficients ---
MAX_REALISTIC_TREE_HEIGHT_M = 50.0
MAX_REALISTIC_CROWN_AREA_SQM = 500.0
MIN_REALISTIC_TREE_HEIGHT_M = 0.5
MIN_REALISTIC_CROWN_AREA_SQM = 0.5
DBH_FROM_HEIGHT_SLOPE = 0.3
DBH_FROM_HEIGHT_INTERCEPT = 0.1
WOOD_DENSITY_RHO = 0.65
ALLOM_COEFF_A = 0.1
ALLOM_COEFF_B = 2.46
BGB_TO_AGB_RATIO = 0.5
CARBON_FRACTION = 0.47
CO2_CONVERSION_FACTOR = 3.67

# --- Celery Task Chain ---
@celery_app.task
def start_processing_pipeline(project_id: int):
    db = get_db()
    update_project_status(db, project_id, "PROCESSING: PHOTOGRAMMETRY")
    db.close()
    
    pipeline = (
        run_photogrammetry.s(project_id) |
        generate_chm.s() |
        segment_trees.s() |
        calculate_carbon.s()
    ).on_error(handle_error.s(project_id))
    
    pipeline.delay()

@celery_app.task
def handle_error(request, exc, traceback, project_id):
    db = get_db()
    update_project_status(db, project_id, f"FAILED: {str(exc)}")
    db.close()
    print(f"Pipeline failed for project {project_id}: {exc}")

# --- Individual Processing Tasks ---

@celery_app.task(bind=True)
def run_photogrammetry(self, project_id: int) -> dict:
    # THIS TASK IS CURRENTLY A SIMULATION FOR PROTOTYPING SPEED
    # To enable real processing, replace this with the WebODM API version.
    print(f"[{project_id}] SIMULATING photogrammetry...")
    data_dir = os.getenv("DATA_DIRECTORY")
    
    # CORRECT: Path is built using the unique project_id
    project_dir = os.path.join(data_dir, str(project_id))
    sample_dir = os.path.join(data_dir, "sample_odm_outputs")

    dsm_path = os.path.join(project_dir, "dsm.tif")
    shutil.copy(os.path.join(sample_dir, "odm_dem.tif"), dsm_path)
    
    db = get_db()
    update_project_status(db, project_id, "PROCESSING: GENERATING CHM")
    db.close()

    return {"project_id": project_id, "dsm_path": dsm_path}

@celery_app.task
def generate_chm(previous_task_result: dict) -> dict:
    project_id = previous_task_result["project_id"]
    dsm_path = previous_task_result["dsm_path"]
    
    # CORRECT: We get the project directory from the path passed by the previous task
    project_dir = os.path.dirname(dsm_path)
    print(f"[{project_id}] Generating CHM from DSM at {dsm_path}...")
    
    dtm_path = os.path.join(project_dir, "dtm.tif")
    low_res_path = os.path.join(project_dir, "dsm_low_res.tif")

    with rasterio.open(dsm_path) as src:
        profile = src.profile

    gdal.Warp(low_res_path, dsm_path, xRes=5, yRes=5, resampleAlg='near')
    gdal.Warp(dtm_path, low_res_path, width=profile['width'], height=profile['height'], resampleAlg='cubic')

    chm_path = os.path.join(project_dir, "chm.tif")
    ds_dsm = gdal.Open(dsm_path)
    ds_dtm = gdal.Open(dtm_path)
    chm_array = ds_dsm.ReadAsArray() - ds_dtm.ReadAsArray()
    
    driver = gdal.GetDriverByName('GTiff')
    ds_chm = driver.Create(chm_path, ds_dsm.RasterXSize, ds_dsm.RasterYSize, 1, gdal.GDT_Float32)
    ds_chm.SetGeoTransform(ds_dsm.GetGeoTransform())
    ds_chm.SetProjection(ds_dsm.GetProjection())
    ds_chm.GetRasterBand(1).WriteArray(chm_array)
    ds_chm.FlushCache()
    ds_dsm = ds_dtm = ds_chm = None

    db = get_db()
    update_project_status(db, project_id, "PROCESSING: SEGMENTING TREES")
    db.close()
    
    return {"project_id": project_id, "chm_path": chm_path}

@celery_app.task
def segment_trees(previous_task_result: dict) -> dict:
    project_id = previous_task_result["project_id"]
    chm_path = previous_task_result["chm_path"]
    project_dir = os.path.dirname(chm_path)
    print(f"[{project_id}] Starting tree segmentation...")
    
    with rasterio.open(chm_path) as src:
        chm = src.read(1)
        transform = src.transform
        crs = src.crs
    
    chm[chm < 1] = 0

    scale_factor = 0.5 
    chm_small = rescale(chm, scale_factor, anti_aliasing=True, preserve_range=True)
    new_transform = transform * transform.scale(scale_factor)
    
    chm_smooth = ndi.gaussian_filter(chm_small, sigma=2)
    local_max_coords = peak_local_max(chm_smooth, min_distance=3, labels=chm_small > 0, exclude_border=False)
    
    markers = np.zeros_like(chm_small, dtype=np.int32)
    markers[tuple(local_max_coords.T)] = np.arange(len(local_max_coords)) + 1
    
    labels = watershed(-chm_smooth, markers, mask=chm_small > 0)
    
    polygons = []
    for label_id in np.unique(labels):
        if label_id == 0: continue
        mask_array = labels == label_id
        shapes_gen = features.shapes(mask_array.astype(np.int16), mask=mask_array, transform=new_transform)
        for geom, val in shapes_gen:
            if shape(geom).area > 2:
                polygons.append({'geometry': shape(geom), 'label_id': label_id})

    crowns_path = os.path.join(project_dir, "tree_crowns.gpkg")
    if polygons:
        gdf = gpd.GeoDataFrame(polygons, crs=crs)
        gdf.to_file(crowns_path, driver="GPKG")
    
    db = get_db()
    update_project_status(db, project_id, "PROCESSING: CALCULATING CARBON", data={"crowns_path": crowns_path})
    db.close()

    return {"project_id": project_id, "chm_path": chm_path, "crowns_path": crowns_path}

@celery_app.task
def calculate_carbon(previous_task_result: dict) -> dict:
    project_id = previous_task_result["project_id"]
    chm_path = previous_task_result["chm_path"]
    crowns_path = previous_task_result["crowns_path"]
    project_dir = os.path.dirname(chm_path)
    print(f"[{project_id}] Starting carbon calculation...")

    try:
        crowns_gdf = gpd.read_file(crowns_path)
        chm_src = rasterio.open(chm_path)
    except Exception as e:
        raise ValueError(f"Could not load files for calculation: {e}")

    tree_data = []
    for index, row in crowns_gdf.iterrows():
        try:
            out_image, out_transform = mask.mask(chm_src, [row.geometry], crop=True, filled=False)
            height = np.nanmax(out_image) if out_image.size > 0 and np.any(np.isfinite(out_image)) else 0
            crown_area = row.geometry.area
            tree_data.append({'tree_id': row['label_id'], 'height_m': height, 'crown_area_sqm': crown_area})
        except Exception as e:
            print(f"Error extracting metrics for tree {row['label_id']}: {e}")
            continue
    
    chm_src.close()
    
    if not tree_data:
        raise ValueError("No trees found after initial metric extraction.")

    df = pd.DataFrame(tree_data)
    
    print("\n--- Tree Dimension Statistics (Before Filtering) ---")
    print(df[['height_m', 'crown_area_sqm']].describe())
    print("----------------------------------------------------\n")
    
    df_filtered = df.query(
        f"{MIN_REALISTIC_CROWN_AREA_SQM} <= crown_area_sqm <= {MAX_REALISTIC_CROWN_AREA_SQM} and "
        f"{MIN_REALISTIC_TREE_HEIGHT_M} <= height_m <= {MAX_REALISTIC_TREE_HEIGHT_M}"
    ).copy()
    
    if df_filtered.empty:
        raise ValueError("No valid trees found after filtering. Adjust filter parameters if this is unexpected.")

    df_filtered['estimated_dbh_cm'] = (DBH_FROM_HEIGHT_SLOPE * df_filtered['height_m']) + DBH_FROM_HEIGHT_INTERCEPT
    df_filtered['agb_kg'] = ALLOM_COEFF_A * (WOOD_DENSITY_RHO * (df_filtered['estimated_dbh_cm'] ** ALLOM_COEFF_B))
    df_filtered['total_biomass_kg'] = df_filtered['agb_kg'] * (1 + BGB_TO_AGB_RATIO)
    df_filtered['carbon_kg'] = df_filtered['total_biomass_kg'] * CARBON_FRACTION
    df_filtered['co2_sequestered_kg'] = df_filtered['carbon_kg'] * CO2_CONVERSION_FACTOR

    carbon_results_path = os.path.join(project_dir, "carbon_inventory.csv")
    df_filtered.to_csv(carbon_results_path, index=False)
    
    total_co2_tonnes = float(df_filtered['co2_sequestered_kg'].sum() / 1000)
    
    db = get_db()
    update_project_status(db, project_id, "COMPLETED", data={"carbon_results_path": carbon_results_path, "total_co2_tonnes": total_co2_tonnes})
    db.close()

    print(f"[{project_id}] Calculation complete. Total CO2: {total_co2_tonnes:.2f} tonnes.")
    return {"project_id": project_id, "total_co2_tonnes": total_co2_tonnes}