import logging

from app.config import settings
from app.odoo_client import OdooClient
from app.arcgis_client import ArcGISClient
from app.schemas import SyncSummary, ReverseSyncSummary, ReconciliationSummary

logger = logging.getLogger("integration.sync")

odoo = OdooClient()
arcgis = ArcGISClient()

_last_summary: SyncSummary | None = None
_last_reverse_summary: ReverseSyncSummary | None = None
_last_reconciliation_summary: ReconciliationSummary | None = None


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
    correspondiente en Odoo, escribiendo de vuelta el vínculo Y el
    status inicial en la misma pasada (ver link_new_feature).

    Idempotente por diseño: antes de tocar Odoo, cada feature se
    "reclama" (claim_feature) escribiendo un valor centinela en
    odoo_partner_id. Si otra corrida de este mismo proceso se solapa
    (el scheduler y un curl manual al mismo tiempo, por ejemplo), ya no
    va a ver esta feature como "sin vincular" y no va a crear una
    segunda tarea para ella. Si la creación en Odoo falla después del
    claim, se revierte (release_claim) para que se reintente en la
    próxima corrida en vez de quedar huérfana para siempre.
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
        oid = record["_oid"]

        try:
            claimed = arcgis.claim_feature(oid)
            if not claimed:
                logger.warning(
                    "object_id=%s: no se pudo reclamar la feature (probablemente "
                    "ya la tomó otra corrida); se omite en esta pasada.",
                    oid,
                )
                continue
        except Exception as exc:  # noqa: BLE001
            msg = f"object_id={oid}: error reclamando feature: {exc}"
            logger.error(msg)
            errors.append(msg)
            continue

        try:
            task_id = odoo.create_task_from_arcgis(record, settings.odoo_project_name)
            status_label = odoo.get_task_state_label(task_id)
            arcgis.link_new_feature(oid, task_id, status_label)
            created += 1
            logger.info("Tarea creada en Odoo desde ArcGIS: task_id=%s", task_id)
        except Exception as exc:  # noqa: BLE001
            msg = f"object_id={oid}: {exc}"
            logger.error(msg)
            errors.append(msg)
            try:
                arcgis.release_claim(oid)
            except Exception as release_exc:  # noqa: BLE001
                logger.error(
                    "Además falló revirtiendo el claim de object_id=%s: %s",
                    oid, release_exc,
                )

    summary = ReverseSyncSummary(ok=len(errors) == 0, created_in_odoo=created, errors=errors)
    _last_reverse_summary = summary
    return summary


def run_reconciliation() -> ReconciliationSummary:
    """
    Detecta features en ArcGIS cuyo odoo_partner_id apunta a una tarea
    que ya no existe en Odoo (fue borrada directamente ahí, por ejemplo
    al limpiar un registro de prueba duplicado) y borra esa feature de
    la capa. Lo que hay en Odoo es la fuente de verdad; ArcGIS debe
    reflejar exactamente ese conjunto, sin referencias fantasma.

    No toca features "reclamadas" (centinela de claim) -- esas están
    siendo procesadas por un reverse-sync en curso, no son huérfanas.
    """
    global _last_reconciliation_summary

    errors: list[str] = []

    try:
        linked = arcgis.get_linked_features()
    except Exception as exc:  # noqa: BLE001
        logger.error("Fallo leyendo features vinculadas: %s", exc)
        summary = ReconciliationSummary(ok=False, checked=0, orphaned_deleted=0, errors=[str(exc)])
        _last_reconciliation_summary = summary
        return summary

    task_ids = {f["task_id"] for f in linked}
    try:
        existing_ids = odoo.filter_existing_task_ids(list(task_ids))
    except Exception as exc:  # noqa: BLE001
        msg = f"Fallo verificando tareas existentes en Odoo: {exc}"
        logger.error(msg)
        summary = ReconciliationSummary(ok=False, checked=len(linked), orphaned_deleted=0, errors=[msg])
        _last_reconciliation_summary = summary
        return summary

    orphaned_oids = [f["_oid"] for f in linked if f["task_id"] not in existing_ids]

    deleted = 0
    if orphaned_oids:
        try:
            deleted = arcgis.delete_features(orphaned_oids)
            logger.info(
                "Reconciliación: %s feature(s) huérfana(s) borrada(s) de ArcGIS "
                "(su task_id ya no existe en Odoo)",
                deleted,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"Fallo borrando features huérfanas {orphaned_oids}: {exc}"
            logger.error(msg)
            errors.append(msg)

    summary = ReconciliationSummary(
        ok=len(errors) == 0, checked=len(linked), orphaned_deleted=deleted, errors=errors,
    )
    _last_reconciliation_summary = summary
    return summary


def get_last_summary() -> SyncSummary | None:
    return _last_summary


def get_last_reverse_summary() -> ReverseSyncSummary | None:
    return _last_reverse_summary


def get_last_reconciliation_summary() -> ReconciliationSummary | None:
    return _last_reconciliation_summary


def handle_arcgis_status_webhook(odoo_task_id: int, new_status: str | None, note: str | None) -> None:
    """
    Aplica un cambio de estado recibido desde ArcGIS (webhook real de la
    Feature Layer, o simulado con curl para demo) a la tarea
    correspondiente en Odoo: mueve la etapa si el nombre coincide, y
    siempre deja constancia en el chatter.
    """
    if new_status:
        moved = odoo.set_task_state_by_label(odoo_task_id, new_status)
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