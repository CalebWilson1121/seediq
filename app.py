import plss_kansas_patch  # must load before server/parser imports

from server import app
from large_upload_routes import router as large_upload_router

app.include_router(large_upload_router)
