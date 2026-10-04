# SeedIQ deployment

## Why a static host gives `405 Not Allowed`
`data-hub.html` sends a `POST` request to `/api/documents/upload`. GitHub Pages and other static-only hosting serve files, but they do not run `server.py`, so they cannot accept that POST request.

The APH file is not the cause of a 405. The API simply is not running on that host.

## Deploy the full application
The repository includes three equivalent server deployment options:

### Render Blueprint
Connect the GitHub repository to Render and use the included `render.yaml`. Render will run:

`uvicorn server:app --host 0.0.0.0 --port $PORT`

Then open `/data-hub.html` on the Render URL. The status box should say **Farm Data Engine connected**.

### Docker
Build the included Dockerfile and deploy it on a host that supports containers.

### Local
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --reload
```
Open `http://127.0.0.1:8000/data-hub.html`.

## Prototype storage note
This starter uses SQLite and a local `uploads/` directory. That is fine for development and a demo. Production should move the database to Postgres and source documents to private object storage (for example S3-compatible storage), with encryption and tenant access controls.
