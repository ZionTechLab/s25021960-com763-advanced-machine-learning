import streamlit as st
import pandas as pd
import numpy as np
import joblib
import os

# Set global page configuration
st.set_page_config(
    page_title="Sri Lankan Used Vehicle Price Predictor (2026)",
    page_icon="🚗",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 1. Load Serialized Pipeline Model ---
MODEL_PATH = "models/vehicle_pricing_pipeline.pkl"

@st.cache_resource
def load_pricing_model():
    """Ingests and caches the unified pipeline object to memory."""
    if not os.path.exists(MODEL_PATH):
        st.error(f"❌ Model artifact missing at `{MODEL_PATH}`. Ensure the pipeline file is committed to your repository.")
        st.stop()
    return joblib.load(MODEL_PATH)

try:
    model_pipeline = load_pricing_model()
except Exception as e:
    st.error(f"💥 Failed to deserialize pipeline binary: {str(e)}")
    st.stop()

# --- 2. Define Valid Categories (Matching Imputation & Cardinality Thresholds) ---
# Hardcoded to ensure perfect mapping alignment across UI dropdown dropdown inputs.
VALID_BRANDS = sorted([
    "TOYOTA", "HONDA", "NISSAN", "SUZUKI", "MITSUBISHI", 
    "MAZDA", "BENZ", "BMW", "KIA", "HYUNDAI", "OTHER"
])

VALID_FUEL_TYPES = ["Petrol", "Diesel", "Hybrid", "Electric", "Other"]
VALID_TRANSMISSIONS = ["Automatic", "Manual", "Tiptronic", "Other"]

VALID_LOCATIONS = sorted([
    "Colombo", "Gampaha", "Kandy", "Kurunegala", "Kalutara", 
    "Galle", "Matara", "Jaffna", "Anuradhapura", "Other"
])

# --- 3. Initialize Session State for Widget Management ---
# Using state handles to cleanly clear forms via button callbacks
if "form_reset" not in st.session_state:
    st.session_state.form_reset = False

def clear_form_callback():
    """Resets predictive features to defaults inside session state."""
    st.session_state["ui_brand"] = "TOYOTA"
    st.session_state["ui_model"] = ""
    st.session_state["ui_year"] = 2018
    st.session_state["ui_mileage"] = 60000
    st.session_state["ui_fuel"] = "Petrol"
    st.session_state["ui_transmission"] = "Automatic"
    st.session_state["ui_engine"] = 1500
    st.session_state["ui_location"] = "Colombo"
    st.session_state["ui_source"] = "ikman.lk"

# Pre-populate session defaults if absent
if "ui_brand" not in st.session_state:
    clear_form_callback()

# --- 4. User Interface Architecture Layout ---
st.title("🚗 Sri Lankan Used Vehicle Price Predictor")
st.markdown("""
This advanced machine learning application predicts the fair market value of used vehicles in Sri Lanka. 
The system runs an optimized tree ensemble algorithm trained on **real-time 2026 market variations**, accounting for recent macroeconomic shifts and tax structural updates.
""")
st.write("---")

# Main split layout: Inputs on the left, Predictions/Analytics on the right
col_inputs, col_outputs = st.columns([1.2, 1])

with col_inputs:
    st.subheader("🛠️ Vehicle Parameter Specifications")
    
    with st.expapnded("Core Specifications", expanded=True):
        c1, col2 = st.columns(2)
        with c1:
            brand = st.selectbox("Brand Manufacturer", VALID_BRANDS, key="ui_brand")
            model = st.text_input("Model Variant (e.g., VITZ, AXIO, CIVIC)", key="ui_model", help="Will be standardized automatically.").upper().strip()
            if not model:
                model = "OTHER"
        with col2:
            year = st.slider("Year of Manufacture", min_value=1980, max_value=2026, key="ui_year")
            # Dynamic generation of vehicle age to match feature transformation matrices
            vehicle_age = 2026 - year

    with st.expander("Mechanical & Powertrain Descriptors", expanded=True):
        col3, col4 = st.columns(2)
        with col3:
            transmission = st.selectbox("Transmission Configuration", VALID_TRANSMISSIONS, key="ui_transmission")
            fuel_type = st.selectbox("Fuel Source Type", VALID_FUEL_TYPES, key="ui_fuel")
        with col4:
            engine_cc = st.number_input("Engine Capacity (cc)", min_value=600, max_value=6000, step=100, key="ui_engine")
            mileage_km = st.number_input("Odometer Mileage (km)", min_value=0, max_value=600000, step=5000, key="ui_mileage")
            # Compute parallel log feature as built inside structural pipeline layer
            log_mileage = np.log1p(mileage_km)

    with st.expander("Operational & Metadata Parameters", expanded=True):
        col5, col6 = st.columns(2)
        with col5:
            location = st.selectbox("Geographic Location", VALID_LOCATIONS, key="ui_location")
        with col6:
            source_site = st.radio("Sourced Data Feed Baseline", ["ikman.lk", "riyasewana.com"], key="ui_source", horizontal=True)

    # Operational trigger panel
    st.write("")
    btn_predict, btn_clear = st.columns([2, 1])
    with btn_predict:
        trigger_prediction = st.button("🔮 Evaluate Fair Market Valuation", type="primary", use_container_width=True)
    with btn_clear:
        st.button("🔄 Reset Parameters", on_click=clear_form_callback, use_container_width=True)

# --- 5. Data Re-Structuring & Inference Processing ---
with col_outputs:
    st.subheader("📊 Valuation Output & Market Analytics")
    
    if trigger_prediction:
        # Build raw runtime record mapping exactly to pipeline feature schema arrays
        input_data = pd.DataFrame([{
            'source_site': source_site,
            'brand': brand,
            'model': model,
            'year': int(year),
            'vehicle_age': int(vehicle_age),
            'mileage_km': float(mileage_km),
            'log_mileage': float(log_mileage),
            'transmission': transmission,
            'fuel_type': fuel_type,
            'engine_cc': float(engine_cc),
            'location': location
        }])
        
        with st.spinner("Processing structural transformations and calculating inference..."):
            try:
                # Execution of prediction via native cached object
                # Evaluates string transformations via ColumnTransformer instantly
                predicted_log_price = model_pipeline.predict(input_data)[0]
                
                # Reverse scaling transformation back to raw LKR scale
                predicted_lkr_price = np.expm1(predicted_log_price)
                
            except Exception as predict_error:
                st.error(f"Inference failure encountered: {str(predict_error)}")
                st.info("💡 Hint: This usually means the structured model value string did not match training schema boundaries.")
                st.stop()
        
        # Display the formatted valuation
        st.balloons()
        st.success("✅ Price Evaluation Generation Complete!")
        
        # Custom Metric Card Layout
        st.markdown(f"""
        <div style="background-color:#1e293b; padding:25px; border-radius:15px; border-left: 8px solid #06b6d4; text-align:center;">
            <p style="color:#94a3b8; font-size:16px; font-weight:bold; margin-bottom:0px; text-transform:uppercase;">Estimated Fair Market Price</p>
            <h1 style="color:#22d3ee; font-size:42px; margin-top:5px; margin-bottom:5px;">LKR {predicted_lkr_price:,.2f}</h1>
            <p style="color:#64748b; font-size:12px; font-style:italic;">*Calculated based on 2026 import relaxation structures and current supply-demand curves.</p>
        </div>
        """, unsafe_url_allowed=True)
        
        # Supplemental Domain Analytics
        st.write("")
        st.markdown("### 📋 Predictive Parameter Context Breakdown")
        
        # Present a neat horizontal data frame summary of what was parsed
        summary_view = pd.DataFrame({
            "Specification Variable": ["Vehicle Age", "Odometer Metrics", "Engine Configuration", "Platform Baseline"],
            "Parsed Value": [f"{vehicle_age} Years", f"{mileage_km:,} km", f"{engine_cc:,} cc", source_site]
        })
        st.table(summary_view)
        
        st.caption("⚠️ Disclamer: Predictions are generated statistically from empirical scraped listing datasets and do not override professional physical automotive assessments.")
    else:
        # State display prior to evaluation trigger
        st.info("💡 Adjust vehicle parameter vectors on the left panel and click 'Evaluate Fair Market Valuation' to generate a real-time price estimation.")
        
        # High-impact placeholder card
        st.markdown("""
        <div style="border: 2px dashed #475569; padding: 40px; border-radius: 15px; text-align: center; color: #64748b;">
            <span style="font-size: 48px;">🔮</span>
            <p style="margin-top: 15px; font-size: 16px;">Awaiting Model Execution Trigger...</p>
        </div>
        """, unsafe_url_allowed=True)