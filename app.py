import plss_kansas_patch  # must load before server/parser imports
import soil_batch_patch  # must patch soil_service before server imports enrich_prospect

from server import app
from large_upload_routes import router as large_upload_router

app.include_router(large_upload_router)
