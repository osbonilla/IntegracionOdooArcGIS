import logging

from fastapi import FastAPI, HTTPException
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.schemas import SyncSummary, ReverseSyncSummary, ArcGISWebhookPayload
from app import sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("integration.main")

app = FastAPI(
    title="Odoo <-> ArcGIS Integration API",
    description=(
        "Microservicio que sincroniza solicitudes ciudadanas "
        "(project.task + res.partner) de Odoo hacia una Hosted Feature "
        "Layer en ArcGIS Online / Enterprise, y procesa actualizaciones "
        "en sentido inverso: features nuevas (p. ej. desde Survey123) y "
        "cambios de estado."
    ),
    version="2.0.0",
)

scheduler = BackgroundScheduler()


@app.on_event("startup")
def startup() -> None:
    if settings.sync_interval_minutes > 0:
        scheduler.add_job(
            sync.run_sync, "interval",
            minutes=settings.sync_interval_minutes, id="periodic_sync",
        )
        scheduler.add_job(
            sync.run_reverse_sync, "interval",
            minutes=settings.sync_interval_minutes, id="periodic_reverse_sync",
        )
        scheduler.start()
        logger.info(
            "Sincronización automática (ambas direcciones) activada cada %s minutos",
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
    """Fuerza una sincronización Odoo -> ArcGIS bajo demanda."""
    try:
        return sync.run_sync()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo el sync Odoo -> ArcGIS")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/sync/last", response_model=SyncSummary | None)
def last_sync() -> SyncSummary | None:
    return sync.get_last_summary()


@app.post("/sync/reverse", response_model=ReverseSyncSummary)
def trigger_reverse_sync() -> ReverseSyncSummary:
    """Fuerza una sincronización ArcGIS -> Odoo bajo demanda (Survey123, ediciones manuales)."""
    try:
        return sync.run_reverse_sync()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo el sync ArcGIS -> Odoo")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/sync/reverse/last", response_model=ReverseSyncSummary | None)
def last_reverse_sync() -> ReverseSyncSummary | None:
    return sync.get_last_reverse_summary()


@app.post("/webhook/arcgis")
def arcgis_webhook(payload: ArcGISWebhookPayload) -> dict:
    """
    Recibe cambios de estado desde ArcGIS (Webhook real de la Feature
    Layer, o simulado con curl para demo — ver README sección 6).
    """
    try:
        sync.handle_arcgis_status_webhook(
            payload.odoo_task_id, payload.new_status, payload.note
        )
        return {"status": "applied"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo aplicando webhook de ArcGIS")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/webhook/odoo")
def odoo_webhook() -> dict:
    """
    Disparado por una Automation Rule de Odoo al crear/editar una tarea
    (acción nativa "Enviar notificación webhook"). Ejecuta el sync
    completo Odoo -> ArcGIS — ver README sección 5.6.
    """
    try:
        summary = sync.run_sync()
        return {"status": "applied", "summary": summary.model_dump()}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fallo el sync disparado por webhook de Odoo")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
