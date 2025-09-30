# frontend.py
import streamlit as st
import requests
import os
import time

# --- Configuration ---
BACKEND_URL = "https://egotistic-elouise-nonmythical.ngrok-free.dev" # PASTE YOUR NGROK URL HERE

st.set_page_config(layout="wide")
st.title("Drone Imagery Carbon Sequestration Platform")

# --- 1. Create a New Project ---
st.header("Step 1: Create a New Project")
project_name = st.text_input("Enter a name for your new project (e.g., Assam_Plot_01):")

if st.button("Create Project"):
    if project_name:
        try:
            response = requests.post(f"{BACKEND_URL}/projects/", json={"name": project_name})
            if response.status_code == 200:
                project_data = response.json()
                st.session_state['project_id'] = project_data['id'] # Save the ID for later
                st.success(f"Project '{project_data['name']}' created with ID: **{project_data['id']}**. You can now upload images below.")
            else:
                st.error(f"Error creating project: {response.text}")
        except requests.exceptions.ConnectionError:
            st.error("Connection Error: Could not connect to the backend. Is it running?")
    else:
        st.warning("Please enter a project name.")


# --- 2. Upload Images to a Project ---
st.header("Step 2: Upload Images")
if 'project_id' in st.session_state:
    st.write(f"Uploading images for Project ID: **{st.session_state['project_id']}**")
    uploaded_files = st.file_uploader(
        "Choose your drone images (JPG recommended for demo)",
        accept_multiple_files=True,
        type=['jpg', 'jpeg', 'png', 'tif', 'tiff']
    )

    if st.button("Upload and Start Processing"):
        if uploaded_files:
            st.info(f"Uploading {len(uploaded_files)} files... This may take a moment.")
            files_to_send = [("files", (file.name, file.getvalue(), file.type)) for file in uploaded_files]

            try:
                response = requests.post(f"{BACKEND_URL}/projects/{st.session_state['project_id']}/upload-images/", files=files_to_send)
                if response.status_code == 200:
                    st.success("✅ Upload complete! Processing has started in the background.")
                    st.info("You can monitor the status in Step 3.")
                else:
                    st.error(f"Error during upload: {response.text}")
            except requests.exceptions.ConnectionError:
                st.error("Connection Error: Could not connect to the backend.")
        else:
            st.warning("Please select files to upload.")
else:
    st.info("Create a project in Step 1 first.")

# --- 3. Check Project Status ---
st.header("Step 3: Monitor Project Status")
project_id_to_check = st.number_input("Enter Project ID to check:", min_value=1, step=1, value=st.session_state.get('project_id', 1))

if st.button("Refresh Status"):
    try:
        response = requests.get(f"{BACKEND_URL}/projects/{project_id_to_check}")
        if response.status_code == 200:
            data = response.json()
            st.write(f"**Project Name:** `{data['name']}`")
            st.write(f"**Status:** `{data['status']}`")
            if data['status'] == 'COMPLETED':
                st.metric(label="Total CO₂ Sequestered (Tonnes)", value=f"{data['total_co2_tonnes']:.8f}")
                st.balloons()
        else:
            st.error("Project not found.")
    except requests.exceptions.ConnectionError:
        st.error("Connection Error: Could not connect to the backend.")