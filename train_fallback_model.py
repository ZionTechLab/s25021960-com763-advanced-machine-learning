import pandas as pd
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error
import joblib

p = Path('data/processed/cleaned_cars.csv')
df = pd.read_csv(p)
features = ['source_site','brand','model','year','vehicle_age','mileage_km','log_mileage','transmission','fuel_type','engine_cc','location']
X = df[features].copy()
y = df['log_price']

categorical = ['source_site','brand','model','transmission','fuel_type','location']
numeric = ['year','vehicle_age','mileage_km','log_mileage','engine_cc']

preprocess = ColumnTransformer([
    ('num', Pipeline([('imputer', SimpleImputer(strategy='median')), ('scaler', StandardScaler())]), numeric),
    ('cat', Pipeline([('imputer', SimpleImputer(strategy='most_frequent')), ('onehot', OneHotEncoder(handle_unknown='ignore'))]), categorical),
])

model = Pipeline([
    ('preprocess', preprocess),
    ('regressor', RandomForestRegressor(n_estimators=200, random_state=42, n_jobs=-1))
])

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
model.fit(X_train, y_train)
preds = model.predict(X_test)
print('MAE:', mean_absolute_error(y_test, preds))
joblib.dump(model, 'models/fallback_vehicle_pricing_model.pkl')
print('Saved fallback model to models/fallback_vehicle_pricing_model.pkl')
