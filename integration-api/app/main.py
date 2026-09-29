import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import sync
from app.config import settings
from app.schemas import ArcGISWebhookPayload, PipelineSummary, RunnerStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("integration.main")

app = FastAPI(
    title="Odoo <-> ArcGIS Integration API",
    description=(
        "Sincronización bidireccional entre Odoo (project.task) y ArcGIS "
        "Online / Enterprise: solicitudes ciudadanas (puntos, Survey123 / "
        "Field Maps) y frentes de trabajo (líneas, Field Maps). Webhooks de "
        "ArcGIS y de Odoo disparan pasadas idempotentes; un scheduler hace "
        "de red de seguridad."
    ),
    version="3.0.0",
)

# El webhook de Survey123 lo envía la propia app (web o de campo); si sale
# del navegador, necesita que la API acepte peticiones de otro origen (CORS).
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"],
                   allow_methods=["GET", "POST", "OPTIONS"])

scheduler = BackgroundScheduler()

# Cabeceras donde ArcGIS envía la firma HMAC-SHA256 ("sha256=<hex>"). La
# documentación de Esri usa ambos nombres según la página; se aceptan los dos.
SIGNATURE_HEADERS = ("x-esrihook-signature", "x-esri-signature")
ARCGIS_LAYER_KEYS = {"solicitudes", "frentes"}


# ---------------------------------------------------------------- seguridad --

def require_api_key(x_api_key: str | None = Header(default=None),
                    api_key: str | None = Query(default=None)) -> None:
    """Protege /sync/* cuando INTEGRATION_API_KEY está definido."""
    if settings.integration_api_key and (x_api_key or api_key) != settings.integration_api_key:
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


def _signature_ok(raw: bytes, request: Request) -> bool:
    secret = settings.arcgis_webhook_secret
    if not secret:
        return True
    received = next((request.headers[h] for h in SIGNATURE_HEADERS if h in request.headers), None)
    if not received:
        return False
    received = received.split("=", 1)[1] if "=" in received else received
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.strip().lower())


def _json_candidates(raw: bytes) -> list:
    """
    Formas en que llega el payload de ArcGIS: JSON directo (lista de
    objetos), un objeto {"payload": "<JSON como texto>"}, o un formulario
    `payload=<JSON>` (content type x-www-form-urlencoded).
    """
    text = raw.decode("utf-8", errors="replace").strip()
    found = []
    try:
        found.append(json.loads(text))
    except ValueError:
        for values in parse_qs(text).values():
            for value in values:
                try:
                    found.append(json.loads(value))
                except ValueError:
                    pass
    unwrapped = []
    for item in found:
        if isinstance(item, dict) and isinstance(item.get("payload"), str):
            try:
                item = json.loads(item["payload"])
            except ValueError:
                pass
        unwrapped.append(item)
    return unwrapped


def _arcgis_events(raw: bytes) -> tuple[bool, list[str]]:
    """(payload_reconocido, eventos)."""
    events: list[str] = []
    recognized = False
    for data in _json_candidates(raw):
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and ("events" in item or "changesUrl" in item):
                recognized = True
                events.extend(str(e) for e in item.get("events") or [])
    return recognized, events


# ---------------------------------------------------------------- ciclo ------

@app.on_event("startup")
def startup() -> None:
    if not settings.integration_api_key:
        logger.warning("INTEGRATION_API_KEY vacío: /sync/* queda abierto. Defínelo si "
                       "expones la API con un túnel.")
    if settings.sync_interval_minutes > 0:
        scheduler.add_job(
            lambda: sync.run_pipeline("scheduler", blocking=False),
            "interval", minutes=settings.sync_interval_minutes, id="pipeline",
            max_instances=1, coalesce=True,
            next_run_time=datetime.now() + timedelta(seconds=15),
        )
        scheduler.start()
        logger.info("Pasada completa programada cada %s min (primera en 15 s).",
                    settings.sync_interval_minutes)
    else:
        logger.info("Scheduler desactivado (SYNC_INTERVAL_MINUTES=0): solo webhooks y /sync/run.")


@app.on_event("shutdown")
def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown()


# ---------------------------------------------------------------- estado -----

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/status", response_model=RunnerStatus)
def status() -> RunnerStatus:
    return RunnerStatus(**sync.runner.status(), last_summary=sync.get_last_summary())


# ------------------------------------------------------------ sync manual ----

@app.post("/sync/run", response_model=PipelineSummary, dependencies=[Depends(require_api_key)])
def trigger_sync() -> PipelineSummary:
    """Pasada completa bajo demanda (espera a que termine y devuelve el resumen)."""
    return sync.run_pipeline("manual /sync/run")


@app.get("/sync/last", response_model=PipelineSummary | None)
def last_sync() -> PipelineSummary | None:
    return sync.get_last_summary()


@app.post("/sync/reverse", response_model=PipelineSummary, dependencies=[Depends(require_api_key)])
def trigger_reverse() -> PipelineSummary:
    """Solo el paso ArcGIS -> Odoo de registros nuevos (Survey123 / Field Maps)."""
    return sync.run_pipeline("manual /sync/reverse", steps=("reverse",))


@app.post("/sync/reconcile", response_model=PipelineSummary, dependencies=[Depends(require_api_key)])
def trigger_reconcile() -> PipelineSummary:
    """Solo la reconciliación: borra features cuya tarea ya no existe en Odoo."""
    return sync.run_pipeline("manual /sync/reconcile", steps=("reconcile",))


@app.post("/sync/assign", response_model=PipelineSummary, dependencies=[Depends(require_api_key)])
def trigger_assign() -> PipelineSummary:
    """Solo la asignación de solicitudes a frentes de trabajo por cercanía."""
    return sync.run_pipeline("manual /sync/assign", steps=("assign",))


# ---------------------------------------------------------------- webhooks ---

@app.post("/webhook/odoo", status_code=202)
def odoo_webhook(token: str | None = Query(default=None)) -> dict:
    """
    Automation Rule de Odoo ("Enviar notificación webhook") al cambiar el
    estado de una tarea. Odoo espera como máximo 1 s y llama ANTES de hacer
    commit, así que se responde de inmediato y la pasada corre después de
    WEBHOOK_DEBOUNCE_SECONDS, cuando el cambio ya es visible.
    """
    if settings.integration_api_key and token != settings.integration_api_key:
        raise HTTPException(status_code=401, detail="token inválido")
    sync.runner.request("odoo")
    return {"status": "accepted"}


@app.get("/webhook/arcgis/{layer_key}")
def arcgis_webhook_probe(layer_key: str) -> dict:
    """Respuesta a comprobaciones de conectividad al registrar el webhook."""
    if layer_key not in ARCGIS_LAYER_KEYS:
        raise HTTPException(status_code=404, detail="capa desconocida")
    return {"status": "ok", "layer": layer_key}


@app.post("/webhook/arcgis/{layer_key}")
async def arcgis_webhook(layer_key: str, request: Request) -> JSONResponse:
    """
    Webhook nativo de la capa (Settings > Webhooks del item, o REST
    createWebhook). Se usa como "timbre": se verifica la firma, se responde
    al instante y se agenda una pasada. El payload no se usa para escribir
    datos, así que un evento repetido o fuera de orden es inofensivo.
    """
    if layer_key not in ARCGIS_LAYER_KEYS:
        raise HTTPException(status_code=404, detail="capa desconocida")
    raw = await request.body()
    recognized, events = _arcgis_events(raw)
    if not raw.strip() or (not recognized and raw.strip().startswith((b"{", b"["))):
        # Prueba de conectividad (ping) al crear el webhook: responder 200.
        return JSONResponse({"status": "pong"})
    if not _signature_ok(raw, request):
        logger.warning("Webhook ArcGIS '%s' con firma inválida o ausente: se descarta.", layer_key)
        raise HTTPException(status_code=401, detail="firma inválida")
    label = "/".join(sorted(set(events))) or "evento"
    logger.info("Webhook de ArcGIS recibido (%s): %s", layer_key, label)
    sync.runner.request(f"arcgis:{layer_key}:{label}")
    return JSONResponse({"status": "accepted", "events": events})


@app.get("/webhook/survey123")
def survey123_probe() -> dict:
    return {"status": "ok"}


@app.post("/webhook/survey123")
async def survey123_webhook(request: Request, token: str | None = Query(default=None)) -> JSONResponse:
    """
    Webhook propio de Survey123 (survey123.arcgis.com > encuesta >
    Configuración > Webhooks). Lo envía la app de Survey123 en el momento del
    envío, sin la espera por lotes de los webhooks de la capa: es la vía más
    rápida para los reportes ciudadanos. Igual que los otros webhooks, solo
    dispara una pasada; el contenido no se usa ni se registra en el log
    (incluye el token del usuario que envió la encuesta).
    """
    if settings.integration_api_key and token != settings.integration_api_key:
        raise HTTPException(status_code=401, detail="token inválido")
    raw = await request.body()
    if not raw.strip():
        return JSONResponse({"status": "pong"})
    event = "envío"
    for data in _json_candidates(raw):
        if isinstance(data, dict) and data.get("eventType"):
            event = str(data["eventType"])
            break
    logger.info("Webhook de Survey123 recibido: %s", event)
    sync.runner.request(f"arcgis:survey123:{event}")
    return JSONResponse({"status": "accepted", "event": event})


@app.post("/webhook/arcgis", dependencies=[Depends(require_api_key)])
def arcgis_manual_webhook(payload: ArcGISWebhookPayload) -> dict:
    """
    Simulación manual (curl) de un cambio de estado hecho en ArcGIS:
    {odoo_task_id, new_status, note}. Aplica en Odoo y agenda una pasada.
    """
    try:
        sync.handle_manual_status(payload.odoo_task_id, payload.new_status, payload.note)
        return {"status": "applied"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falló la simulación /webhook/arcgis")
        raise HTTPException(status_code=502, detail=str(exc)) from exc