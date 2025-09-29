import os
import shutil
from celery import Celery
from sqlalchemy.orm import Session
from osgeo import gdal
import rasterio
from rasterio import features, mask
import numpy as np
from scipy import ndimage as ndi
from skimage.segmentation import watershed
from skimage.feature import peak_local_max
import geopandas as gpd
import pandas as pd
from shapely.geometry import shape
from . import database, models

# Configure Celery
celery_app = Celery(
    "tasks",
    broker="redis://redis:6379/0",
    backend="redis://redis:6379/0"
)

celery_app.conf.update(
    task_track_started=True,
)

# --- HELPER FUNCTIONS ---
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

# --- CELERY TASK CHAIN ---
@celery_app.task
def start_processing_pipeline(project_id: int):
    """The main entry point task that chains all other tasks together."""
    db = get_db()
    update_project_status(db, project_id, "PROCESSING: PHOTOGRAMMETRY")
    db.close()
    
    # Create a chain of tasks that will execute in order
    pipeline = (
        simulate_photogrammetry.s(project_id) |
        generate_chm.s() |
        segment_trees.s() |
        calculate_carbon.s()
    ).on_error(handle_error.s(project_id))
    
    pipeline.delay()

@celery_app.task
def handle_error(request, exc, traceback, project_id):
    """Task to handle errors in the pipeline."""
    db = get_db()
    update_project_status(db, project_id, f"FAILED: {str(exc)}")
    db.close()
    print(f"Pipeline failed for project {project_id}: {exc}")

# --- INDIVIDUAL PROCESSING TASKS ---

@celery_app.task
def simulate_photogrammetry(project_id: int) -> dict:
    """
    SIMULATED: Copies pre-processed WebODM outputs into the project folder.
    In a real system, this would interact with the WebODM API.
    """
    db = get_db()
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    project_name = project.name
    db.close()

    data_dir = os.getenv("DATA_DIRECTORY")
    project_dir = os.path.join(data_dir, project_name)
    sample_dir = os.path.join(data_dir, "sample_odm_outputs")

    dsm_path = os.path.join(project_dir, "dsm.tif")
    
    shutil.copy(os.path.join(sample_dir, "odm_dem.tif"), dsm_path)
    
    db = get_db()
    update_project_status(db, project_id, "PROCESSING: GENERATING CHM")
    db.close()

    return {"project_id": project_id, "dsm_path": dsm_path}

@celery_app.task
def generate_chm(previous_task_result: dict) -> dict:
    """Generates the Canopy Height Model (CHM) from the DSM."""
    project_id = previous_task_result["project_id"]
    dsm_path = previous_task_result["dsm_path"]
    project_dir = os.path.dirname(dsm_path)
    
    # Generate DTM
    dtm_path = os.path.join(project_dir, "dtm.tif")
    low_res_path = os.path.join(project_dir, "dsm_low_res.tif")

    with rasterio.open(dsm_path) as src:
        profile = src.profile

    gdal.Warp(low_res_path, dsm_path, xRes=5, yRes=5, resampleAlg='near')
    gdal.Warp(dtm_path, low_res_path, width=profile['width'], height=profile['height'], resampleAlg='cubic')

    # Generate CHM
    chm_path = os.path.join(project_dir, "chm.tif")
    ds_dsm = gdal.Open(dsm_path)
    ds_dtm = gdal.Open(dtm_path)
    dsm_band = ds_dsm.GetRasterBand(1)
    dtm_band = ds_dtm.GetRasterBand(1)
    
    chm_array = dsm_band.ReadAsArray() - dtm_band.ReadAsArray()
    
    driver = gdal.GetDriverByName('GTiff')
    ds_chm = driver.Create(chm_path, ds_dsm.RasterXSize, ds_dsm.RasterYSize, 1, gdal.GDT_Float32)
    ds_chm.SetGeoTransform(ds_dsm.GetGeoTransform())
    ds_chm.SetProjection(ds_dsm.GetProjection())
    chm_band = ds_chm.GetRasterBand(1)
    chm_band.WriteArray(chm_array)
    chm_band.FlushCache()
    ds_dsm = ds_dtm = ds_chm = None # Close files

    db = get_db()
    update_project_status(db, project_id, "PROCESSING: SEGMENTING TREES", data={"chm_path": chm_path})
    db.close()
    
    return {"project_id": project_id, "chm_path": chm_path}

@celery_app.task
def segment_trees(previous_task_result: dict) -> dict:
    """Segments trees from the CHM and saves them as polygons."""
    project_id = previous_task_result["project_id"]
    chm_path = previous_task_result["chm_path"]
    project_dir = os.path.dirname(chm_path)
    
    with rasterio.open(chm_path) as src:
        chm = src.read(1)
        transform = src.transform
        crs = src.crs
    
    chm[chm < 1] = 0
    chm_smooth = ndi.gaussian_filter(chm, sigma=1.5)
    local_maxi = peak_local_max(chm_smooth, min_distance=3, labels=chm > 0)
    markers = ndi.label(ndi.binary_fill_holes(local_maxi))[0]
    labels = watershed(-chm_smooth, markers, mask=chm > 0)
    
    polygons = []
    for label_id in np.unique(labels):
        if label_id == 0: continue
        mask_array = labels == label_id
        shapes_gen = features.shapes(mask_array.astype(np.int16), mask=mask_array, transform=transform)
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
    """Calculates final carbon sequestration values."""
    project_id = previous_task_result["project_id"]
    chm_path = previous_task_result["chm_path"]
    crowns_path = previous_task_result["crowns_path"]
    project_dir = os.path.dirname(chm_path)

    crowns_gdf = gpd.read_file(crowns_path)
    chm_src = rasterio.open(chm_path)
    
    tree_data = []
    for index, row in crowns_gdf.iterrows():
        out_image, out_transform = mask.mask(chm_src, [row.geometry], crop=True, filled=False)
        height = np.nanmax(out_image) if out_image.size > 0 and np.nanmax(out_image) > 0 else 0
        tree_data.append({
            'tree_id': row['label_id'],
            'height_m': height,
            'crown_area_sqm': row.geometry.area
        })
    df = pd.DataFrame(tree_data).query('height_m > 0')

    # Allometric equations
    wood_density_rho, alpha_dbh, beta_h, gamma_ca = 0.65, 0.3, 1.2, 0.1
    df['dbh_cm'] = alpha_dbh * (df['height_m'] ** beta_h) + (df['crown_area_sqm'] * gamma_ca)
    df['agb_kg'] = 0.1 * (wood_density_rho * (df['dbh_cm'] ** 2.46))
    df['total_biomass_kg'] = df['agb_kg'] * 1.5
    df['carbon_kg'] = df['total_biomass_kg'] * 0.47
    df['co2_sequestered_kg'] = df['carbon_kg'] * 3.67

    carbon_results_path = os.path.join(project_dir, "carbon_inventory.csv")
    df.to_csv(carbon_results_path, index=False)
    
    total_co2_tonnes = df['co2_sequestered_kg'].sum() / 1000

    db = get_db()
    update_project_status(db, project_id, "COMPLETED", data={
        "carbon_results_path": carbon_results_path,
        "total_co2_tonnes": total_co2_tonnes
    })
    db.close()

    return {"project_id": project_id, "total_co2_tonnes": total_co2_tonnes}