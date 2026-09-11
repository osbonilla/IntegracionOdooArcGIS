from typing import Optional
from pydantic import BaseModel


class SyncSummary(BaseModel):
    ok: bool
    fetched_from_odoo: int
    created_in_arcgis: int
    updated_in_arcgis: int
    skipped_no_coordinates: int
    errors: list[str] = []


class ArcGISWebhookPayload(BaseModel):
    """
    Payload simplificado esperado desde un webhook de ArcGIS Online.
    Ver README sección 6 para el formato real de AGOL y cómo adaptarlo.
    """
    odoo_task_id: int
    new_status: Optional[str] = None
    note: Optional[str] = None