from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="智营台 API", version="0.1.0")

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
