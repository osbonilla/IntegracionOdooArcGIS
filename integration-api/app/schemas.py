from datetime import datetime

from pydantic import BaseModel, Field


class LayerCounts(BaseModel):
    """Contadores de una pasada, por capa (solicitudes / frentes)."""
    created_in_odoo: int = 0          # features de campo -> tareas nuevas
    created_in_arcgis: int = 0        # tareas de Odoo -> features nuevas
    updated_in_odoo: int = 0          # estado / ubicación / datos del frente
    updated_in_arcgis: int = 0        # features actualizadas (solo si algo cambió)
    conflicts: int = 0                # ambos lados cambiaron: ganó el más reciente
    rejected_field_values: int = 0    # estado inválido escrito en campo (se revierte)
    notes_posted: int = 0             # observaciones de campo -> chatter
    photos_synced: int = 0            # adjuntos de campo -> Odoo
    orphaned_deleted: int = 0         # features cuya tarea ya no existe
    skipped_no_coordinates: int = 0   # tareas sin ubicación (no van al mapa)


class PipelineSummary(BaseModel):
    ok: bool
    reason: str
    steps: list[str]
    started_at: datetime
    duration_s: float
    solicitudes: LayerCounts = Field(default_factory=LayerCounts)
    frentes: LayerCounts = Field(default_factory=LayerCounts)
    assignment_changes: int = 0       # solicitudes que cambiaron de frente
    errors: list[str] = Field(default_factory=list)


class RunnerStatus(BaseModel):
    running: bool
    pending_reasons: list[str]
    last_summary: PipelineSummary | None


class ArcGISWebhookPayload(BaseModel):
    """Payload del endpoint de simulación manual /webhook/arcgis (curl)."""
    odoo_task_id: int
    new_status: str | None = None
    note: str | None = None