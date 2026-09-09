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
    """
    Flujo principal: Odoo (contactos tageados) -> ArcGIS (Hosted Feature Layer).
    Idempotente: usa odoo_partner_id como llave para decidir create vs update.
    """
    global _last_summary

    created = updated = skipped = 0
    errors: list[str] = []

    partners = odoo.get_tagged_partners(settings.odoo_sync_tag)
    logger.info(
        "Sincronizando %s contactos con tag '%s'", len(partners), settings.odoo_sync_tag
    )

    for partner in partners:
        try:
            outcome = arcgis.upsert_partner(partner)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - queremos capturar y reportar, no tumbar el sync
            msg = f"partner_id={partner.get('id')}: {exc}"
            logger.error(msg)
            errors.append(msg)

    summary = SyncSummary(
        ok=len(errors) == 0,
        fetched_from_odoo=len(partners),
        created_in_arcgis=created,
        updated_in_arcgis=updated,
        skipped_no_coordinates=skipped,
        errors=errors,
    )
    _last_summary = summary
    return summary


def get_last_summary() -> SyncSummary | None:
    return _last_summary


def handle_arcgis_webhook(odoo_partner_id: int, new_status: str | None, note: str | None) -> None:
    """
    Flujo inverso: un cambio hecho en ArcGIS (ej. un operador de campo marca
    una solicitud como "Resuelta" en Field Maps/Dashboard) se refleja en Odoo
    como una nota en el chatter del contacto. En una implementación real esto
    normalmente actualizaría un modelo de negocio (project.task, helpdesk.ticket)
    en lugar de solo dejar una nota; se deja así para no requerir un módulo
    custom de Odoo en la demo.
    """
    parts = []
    if new_status:
        parts.append(f"Nuevo estado desde ArcGIS: <b>{new_status}</b>")
    if note:
        parts.append(note)
    body = "<br/>".join(parts) or "Actualización recibida desde ArcGIS."

    odoo.post_note(odoo_partner_id, body)
    logger.info("Webhook de ArcGIS aplicado a partner_id=%s", odoo_partner_id)
