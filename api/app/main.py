from fastapi import FastAPI

app = FastAPI(title="lucos_photos")


@app.get("/healthcheck")
def healthcheck():
    return {"status": "ok"}
