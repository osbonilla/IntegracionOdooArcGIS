from typing import Optional
from pydantic import BaseModel


class SyncSummary(BaseModel):
    ok: bool
    fetched_from_odoo: int
    created_in_arcgis: int
    updated_in_arcgis: int
    skipped_no_coordinates: int
    errors: list[str] = []


class ReverseSyncSummary(BaseModel):
    ok: bool
    created_in_odoo: int
    errors: list[str] = []


class ArcGISWebhookPayload(BaseModel):
    """
    Payload para aplicar un cambio de estado recibido desde ArcGIS a la
    tarea correspondiente en Odoo. El payload nativo de un Webhook real
    de AGOL/Enterprise difiere de este esquema simplificado — ver README
    sección 6 para cómo adaptarlo.
    """
    odoo_task_id: int
    new_status: Optional[str] = None
    note: Optional[str] = None
