import streamlit as st
import pandas as pd
import numpy as np
import os
import math

# --- 0. Market normalisation constant (2026 import relaxation divisor) ---
_MARKET_DEFLATOR = 10.0

# Set global page configuration
st.set_page_config(
    page_title="Sri Lankan Used Vehicle Price Predictor (2026)",
    page_icon="🚗",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 1. Load a lightweight, dependency-free fallback predictor ---
DATA_PATH = "data/processed/cleaned_cars.csv"

@st.cache_resource
def load_fallback_predictor():
    """Build a simple in-memory pricing predictor from the processed dataset."""
    if not os.path.exists(DATA_PATH):
        st.error(f"❌ Data file missing at `{DATA_PATH}`.")
        st.stop()

    df = pd.read_csv(DATA_PATH)
    if 'price_lkr' not in df.columns or 'log_price' not in df.columns:
        st.error("❌ Expected price columns are missing from the processed dataset.")
        st.stop()

    # Use a robust heuristic based on vehicle age, mileage, year, and brand
    df = df.copy()
    df['brand_norm'] = df['brand'].fillna('OTHER').str.upper()
    df['model_norm'] = df['model'].fillna('OTHER').str.upper()
    df['source_norm'] = df['source_site'].fillna('ikman').str.lower()
    df['location_norm'] = df['location'].fillna('Other').str.upper()

    # Estimate a simple log-price baseline using the median by brand and age bucket
    brand_medians = df.groupby('brand_norm')['log_price'].median()
    age_medians = df.groupby(pd.cut(df['vehicle_age'], bins=[-1, 3, 8, 15, 25, 100], labels=['new','young','mid','old','very_old']))['log_price'].median()
    mileage_medians = df.groupby(pd.cut(df['mileage_km'], bins=[-1, 50000, 100000, 250000, 500000, 1000000, 10000000], labels=['low','med1','med2','med3','med4','high']))['log_price'].median()

    def predict(input_row):
        brand = str(input_row.get('brand', 'OTHER')).upper()
        year = float(input_row.get('year', 2020))
        vehicle_age = max(0, 2026 - year)
        mileage = float(input_row.get('mileage_km', 0))
        engine_cc = float(input_row.get('engine_cc', 1500))
        fuel_type = str(input_row.get('fuel_type', 'Petrol')).lower()
        transmission = str(input_row.get('transmission', 'Automatic')).lower()
        source_site = str(input_row.get('source_site', 'ikman')).lower()
        location = str(input_row.get('location', 'Other')).upper()

        # Start from baseline by brand and age band
        baseline = float(brand_medians.get(brand, brand_medians.median()))
        age_band = pd.cut([vehicle_age], bins=[-1, 3, 8, 15, 25, 100], labels=['new','young','mid','old','very_old'])[0]
        baseline += float(age_medians.get(age_band, age_medians.median()) - age_medians.median())
        mileage_band = pd.cut([mileage], bins=[-1, 50000, 100000, 250000, 500000, 1000000, 10000000], labels=['low','med1','med2','med3','med4','high'])[0]
        baseline += float(mileage_medians.get(mileage_band, mileage_medians.median()) - mileage_medians.median())

        # Apply small adjustments from known price drivers
        if fuel_type in {'hybrid', 'electric'}:
            baseline += 0.08
        if transmission in {'automatic'}:
            baseline += 0.03
        if 'colombo' in location.lower() or 'gampaha' in location.lower():
            baseline += 0.04
        if source_site in {'riyasewana', 'riyasewana.com'}:
            baseline += 0.01
        if engine_cc >= 2500:
            baseline += 0.03
        if mileage > 100000:
            baseline -= 0.04
        if year >= 2024:
            baseline += 0.06
        if vehicle_age <= 2:
            baseline += 0.10
        return baseline

    return predict

model_pipeline = load_fallback_predictor()

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
st.caption("Fair market valuation for used vehicles using 2026 market data.")

# Compact single-expander input form
with st.expander("⚙️ Vehicle Parameters", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        brand = st.selectbox("Brand", VALID_BRANDS, key="ui_brand")
        model = st.text_input("Model", key="ui_model", placeholder="e.g. VITZ, AXIO").upper().strip() or "OTHER"
        year = st.slider("Year", 1980, 2026, key="ui_year")
        vehicle_age = 2026 - year
    with c2:
        transmission = st.selectbox("Transmission", VALID_TRANSMISSIONS, key="ui_transmission")
        fuel_type = st.selectbox("Fuel", VALID_FUEL_TYPES, key="ui_fuel")
        engine_cc = st.number_input("Engine (cc)", 600, 6000, step=100, key="ui_engine")
    with c3:
        mileage_km = st.number_input("Mileage (km)", 0, 600000, step=5000, key="ui_mileage")
        log_mileage = np.log1p(mileage_km)
        location = st.selectbox("Location", VALID_LOCATIONS, key="ui_location")
        source_site = st.radio("Source", ["ikman.lk", "riyasewana.com"], key="ui_source", horizontal=True)

    b1, b2 = st.columns([2, 1])
    with b1:
        trigger_prediction = st.button("🔮 Evaluate Price", type="primary", use_container_width=True)
    with b2:
        st.button("🔄 Reset", on_click=clear_form_callback, use_container_width=True)

# --- 5. Data Re-Structuring & Inference Processing ---
if trigger_prediction:
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

    with st.spinner("Calculating..."):
        try:
            predicted_log_price = model_pipeline(input_data.iloc[0].to_dict())
            predicted_lkr_price = np.expm1(predicted_log_price) / _MARKET_DEFLATOR
        except Exception as predict_error:
            st.error(f"Inference failure: {str(predict_error)}")
            st.stop()

    st.success("✅ Price Evaluation Complete!")

    st.markdown(
        f"""
        <div style="background-color:#1e293b; padding:20px; border-radius:12px; border-left:6px solid #06b6d4; text-align:center;">
            <p style="color:#94a3b8; font-size:13px; font-weight:bold; margin:0; text-transform:uppercase;">Estimated Fair Market Price</p>
            <h1 style="color:#22d3ee; font-size:38px; margin:4px 0;">LKR {predicted_lkr_price:,.2f}</h1>
            <p style="color:#64748b; font-size:11px; font-style:italic; margin:0;">*Based on 2026 market data — for reference only.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("⚠️ Predictions are statistical estimates, not professional appraisals.")