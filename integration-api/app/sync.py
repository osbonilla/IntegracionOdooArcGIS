import logging

from app.config import settings
from app.odoo_client import OdooClient
from app.arcgis_client import ArcGISClient
from app.schemas import SyncSummary, ReverseSyncSummary

logger = logging.getLogger("integration.sync")

odoo = OdooClient()
arcgis = ArcGISClient()

_last_summary: SyncSummary | None = None
_last_reverse_summary: ReverseSyncSummary | None = None


def run_sync() -> SyncSummary:
    """Odoo (project.task + res.partner) -> ArcGIS (Hosted Feature Layer)."""
    global _last_summary

    created = updated = skipped = 0
    errors: list[str] = []

    requests_ = odoo.get_project_tasks(settings.odoo_project_name)
    logger.info(
        "Sincronizando %s solicitudes del proyecto '%s'",
        len(requests_), settings.odoo_project_name,
    )

    for record in requests_:
        try:
            outcome = arcgis.upsert_request(record)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            msg = f"task_id={record.get('task_id')}: {exc}"
            logger.error(msg)
            errors.append(msg)

    summary = SyncSummary(
        ok=len(errors) == 0,
        fetched_from_odoo=len(requests_),
        created_in_arcgis=created,
        updated_in_arcgis=updated,
        skipped_no_coordinates=skipped,
        errors=errors,
    )
    _last_summary = summary
    return summary


def run_reverse_sync() -> ReverseSyncSummary:
    """
    ArcGIS -> Odoo: detecta features sin odoo_partner_id (creadas
    directo en la capa o vía Survey123) y crea la tarea + contacto
    correspondiente en Odoo, escribiendo de vuelta el vínculo.
    """
    global _last_reverse_summary

    created = 0
    errors: list[str] = []

    try:
        unlinked = arcgis.get_unlinked_features()
    except Exception as exc:  # noqa: BLE001
        logger.error("Fallo leyendo features sin vincular: %s", exc)
        summary = ReverseSyncSummary(ok=False, created_in_odoo=0, errors=[str(exc)])
        _last_reverse_summary = summary
        return summary

    for record in unlinked:
        try:
            task_id = odoo.create_task_from_arcgis(record, settings.odoo_project_name)
            arcgis.set_odoo_id(record["_oid"], task_id)
            created += 1
            logger.info("Tarea creada en Odoo desde ArcGIS: task_id=%s", task_id)
        except Exception as exc:  # noqa: BLE001
            msg = f"object_id={record.get('_oid')}: {exc}"
            logger.error(msg)
            errors.append(msg)

    summary = ReverseSyncSummary(ok=len(errors) == 0, created_in_odoo=created, errors=errors)
    _last_reverse_summary = summary
    return summary


def get_last_summary() -> SyncSummary | None:
    return _last_summary


def get_last_reverse_summary() -> ReverseSyncSummary | None:
    return _last_reverse_summary


def handle_arcgis_status_webhook(odoo_task_id: int, new_status: str | None, note: str | None) -> None:
    """
    Aplica un cambio de estado recibido desde ArcGIS (webhook real de la
    Feature Layer, o simulado con curl para demo) a la tarea
    correspondiente en Odoo: mueve la etapa si el nombre coincide, y
    siempre deja constancia en el chatter.
    """
    if new_status:
        moved = odoo.set_task_stage_by_name(odoo_task_id, new_status)
        if not moved:
            logger.warning(
                "No existe una etapa de Odoo llamada '%s'; se registra solo como nota.",
                new_status,
            )

    parts = []
    if new_status:
        parts.append(f"Actualización desde ArcGIS: <b>{new_status}</b>")
    if note:
        parts.append(note)
    body = "<br/>".join(parts) or "Actualización recibida desde ArcGIS."

    odoo.post_note_on_task(odoo_task_id, body)
    logger.info("Webhook de ArcGIS aplicado a task_id=%s", odoo_task_id)
