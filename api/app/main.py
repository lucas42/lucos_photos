import os

from fastapi import FastAPI

app = FastAPI(title="lucos_photos")


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}


@app.get("/_info")
def info():
    return {
        "system": os.environ.get("SYSTEM", "lucos_photos"),
        "checks": {},
        "metrics": {},
        "ci": {
            "circle": "gh/lucas42/lucos_photos",
        },
        "icon": "/icon",
        "network_only": True,
        "title": "Photos",
        "show_on_homepage": True,
        "start_url": "/",
    }
