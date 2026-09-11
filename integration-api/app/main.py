import logging

from fastapi import FastAPI, HTTPException
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.schemas import SyncSummary, ArcGISWebhookPayload
from app import sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("integration.main")

app = FastAPI(
    title="Odoo <-> ArcGIS Integration API",
    version="1.0.0",
)

scheduler = BackgroundScheduler()


@app.on_event("startup")
def startup() -> None:
    if settings.sync_interval_minutes > 0:
        scheduler.add_job(
            sync.run_sync,
            "interval",
            minutes=settings.sync_interval_minutes,
            id="periodic_sync",
        )
        scheduler.start()
        logger.info(
            "Sincronización automática activada cada %s minutos",
            settings.sync_interval_minutes,
        )
    else:
        logger.info("Sincronización automática desactivada (SYNC_INTERVAL_MINUTES=0)")


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/sync/run", response_model=SyncSummary)
def trigger_sync() -> SyncSummary:
    try:
        return sync.run_sync()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo el sync")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/sync/last", response_model=SyncSummary | None)
def last_sync() -> SyncSummary | None:
    return sync.get_last_summary()


@app.post("/webhook/arcgis")
def arcgis_webhook(payload: ArcGISWebhookPayload) -> dict:
    try:
        sync.handle_arcgis_webhook(
            payload.odoo_task_id, payload.new_status, payload.note
        )
        return {"status": "applied"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo aplicando webhook de ArcGIS")
        raise HTTPException(status_code=502, detail=str(exc)) from exc