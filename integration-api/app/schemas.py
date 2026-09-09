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
    Payload simplificado esperado desde un webhook de ArcGIS Online
    (Feature Layer -> Webhook -> FeaturesUpdated / FeaturesCreated).
    Ver README sección "Flujo inverso ArcGIS -> Odoo" para el formato real
    que entrega AGOL y cómo adaptarlo si difiere.
    """
    odoo_partner_id: int
    new_status: Optional[str] = None
    note: Optional[str] = None
