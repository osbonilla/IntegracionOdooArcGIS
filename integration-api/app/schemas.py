"""
No tengo tu schemas.py actual en esta sesión, así que este archivo
asume la forma más probable dado lo que usan main.py/sync.py (pydantic
BaseModel con los mismos tres esquemas que ya mencionaste antes:
SyncSummary, ReverseSyncSummary, ArcGISWebhookPayload). Si tu archivo
real tiene otros campos o imports adicionales, AGREGA solo la clase
ReconciliationSummary de abajo a tu archivo real en vez de reemplazarlo
por este -- este archivo es para referencia/contexto, no para pegar tal
cual si difiere del tuyo.
"""

from pydantic import BaseModel


class SyncSummary(BaseModel):
    ok: bool
    fetched_from_odoo: int
    created_in_arcgis: int
    updated_in_arcgis: int
    skipped_no_coordinates: int
    errors: list[str]


class ReverseSyncSummary(BaseModel):
    ok: bool
    created_in_odoo: int
    errors: list[str]


class ReconciliationSummary(BaseModel):
    """
    Resultado de una pasada de reconciliación: revisa las features
    vinculadas en ArcGIS y borra las que apuntan a una tarea de Odoo
    que ya no existe.
    """
    ok: bool
    checked: int              # features vinculadas revisadas
    orphaned_deleted: int     # features borradas por ser huérfanas
    errors: list[str]


class ArcGISWebhookPayload(BaseModel):
    odoo_task_id: int
    new_status: str | None = None
    note: str | None = None