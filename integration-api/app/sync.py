import logging

from app.config import settings
from app.odoo_client import OdooClient
from app.arcgis_client import ArcGISClient
from app.schemas import SyncSummary

logger = logging.getLogger("integration.sync")

odoo = OdooClient()
arcgis = ArcGISClient()

_last_summary: SyncSummary | None = None


def run_sync() -> SyncSummary:
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


def get_last_summary() -> SyncSummary | None:
    return _last_summary


def handle_arcgis_webhook(odoo_task_id: int, new_status: str | None, note: str | None) -> None:
    parts = []
    if new_status:
        parts.append(f"Nuevo estado desde ArcGIS: <b>{new_status}</b>")
    if note:
        parts.append(note)
    body = "<br/>".join(parts) or "Actualización recibida desde ArcGIS."

    odoo.post_note_on_task(odoo_task_id, body)
    logger.info("Webhook de ArcGIS aplicado a task_id=%s", odoo_task_id)