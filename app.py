import plss_kansas_patch  # must load before server/parser imports
import soil_batch_patch  # must patch soil_service before server imports enrich_prospect
import soi_agronomic_patch  # retain IRR/NIRR agronomic practice from mapped SOI

from server import app
from large_upload_routes import router as large_upload_router
from seed_planning_routes import router as seed_planning_router
from channel_fit_routes import router as channel_fit_router
from proposal_routes import router as proposal_router

app.include_router(large_upload_router)
app.include_router(seed_planning_router)
app.include_router(channel_fit_router)
app.include_router(proposal_router)
