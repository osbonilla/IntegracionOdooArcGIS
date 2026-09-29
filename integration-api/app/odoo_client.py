"""
Cliente Odoo usando XML-RPC (External API estándar de Odoo).

Modelo de datos:
- Solicitud ciudadana = `project.task` en ODOO_PROJECT_NAME, vinculada a un
  contacto (`res.partner`) con la geolocalización
  (`partner_latitude` / `partner_longitude`, módulo `base_geolocalize`).
- Frente de trabajo = `project.task` en ODOO_WORKFRONT_PROJECT_NAME (sin
  contacto; la geometría vive en ArcGIS).
- La asignación solicitud -> frente se expresa con una etiqueta
  (`project.tags`) "Frente #<id> · <nombre>" en la tarea de la solicitud.

El estado se lee/escribe en el campo `state` (Selection) de project.task,
NO en `stage_id` (columna Kanban). `state` guarda un código interno
("01_in_progress"); la etiqueta visible ("In Progress") se resuelve con
fields_get en vez de asumir un mapeo fijo.

Notas de Odoo 17/18 que este archivo respeta:
- message_post escapa el cuerpo si es texto plano; para HTML se envía
  body_is_html=True (Odoo lo registra con un warning inofensivo).
- `04_waiting_normal` ("Waiting") lo calcula Odoo a partir de dependencias:
  no se escribe nunca desde la integración.
"""

from __future__ import annotations

import base64
import logging
import xmlrpc.client
from typing import Any, Iterable

from app.config import settings

logger = logging.getLogger("integration.odoo")

# Estados que la integración nunca escribe (Odoo los calcula).
NON_WRITABLE_STATES = {"04_waiting_normal"}
CANCELLED_STATE = "1_canceled"
FRONT_TAG_PREFIX = "Frente #"


class OdooClient:
    def __init__(self) -> None:
        self.url = settings.odoo_url
        self.db = settings.odoo_db
        self.username = settings.odoo_username
        self.password = settings.odoo_password
        self._uid: int | None = None
        self._state_label_map: dict[str, str] | None = None

    # ------------------------------------------------------------ base --

    def _common(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)

    def _models(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def authenticate(self) -> int:
        if self._uid:
            return self._uid
        uid = self._common().authenticate(self.db, self.username, self.password, {})
        if not uid:
            raise RuntimeError(
                "Autenticación con Odoo falló. Revisa ODOO_DB / ODOO_USERNAME / "
                "ODOO_PASSWORD en integration-api/.env"
            )
        self._uid = uid
        logger.info("Autenticado en Odoo como uid=%s (db=%s)", uid, self.db)
        return uid

    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        Envoltorio sobre execute_kw. args = parámetros posicionales del
        método; kwargs = parámetros keyword (necesarios p. ej. en
        message_post, que solo acepta keywords).
        """
        uid = self.authenticate()
        models = self._models()
        if kwargs:
            return models.execute_kw(self.db, uid, self.password, model, method,
                                     list(args), kwargs)
        return models.execute_kw(self.db, uid, self.password, model, method, list(args))

    # --------------------------------------------------------- proyectos --

    def get_or_create_project(self, project_name: str) -> int:
        ids = self._execute("project.project", "search", [["name", "=", project_name]])
        if ids:
            return ids[0]
        return self._execute("project.project", "create", {"name": project_name})

    def _project_ids(self, project_name: str) -> list[int]:
        return self._execute("project.project", "search", [["name", "=", project_name]])

    # ----------------------------------------------------------- estados --

    def _get_state_label_map(self) -> dict[str, str]:
        if self._state_label_map is None:
            info = self._execute("project.task", "fields_get", ["state"], ["selection"])
            self._state_label_map = dict(info["state"]["selection"])
        return self._state_label_map

    def state_label(self, code: str | None) -> str:
        return self._get_state_label_map().get(code, code or "")

    def state_code(self, label: str | None) -> str | None:
        """Código escribible para una etiqueta; None si no existe o no es escribible."""
        if not label:
            return None
        for code, lbl in self._get_state_label_map().items():
            if lbl == label and code not in NON_WRITABLE_STATES:
                return code
        return None

    def cancelled_label(self) -> str:
        return self.state_label(CANCELLED_STATE) or "Cancelled"

    def set_task_state_code(self, task_id: int, code: str) -> None:
        self._execute("project.task", "write", [task_id], {"state": code})

    def set_task_state_by_label(self, task_id: int, label: str) -> bool:
        code = self.state_code(label)
        if code is None:
            return False
        self.set_task_state_code(task_id, code)
        return True

    def get_task_state_label(self, task_id: int) -> str:
        task = self._execute("project.task", "read", [task_id], ["state"])
        return self.state_label(task[0]["state"] if task else None)

    # ------------------------------------------------------- solicitudes --

    def get_project_tasks(self, project_name: str) -> list[dict]:
        """
        Tareas del proyecto + datos del contacto vinculado (nombre del
        ciudadano y geolocalización). Dos consultas: la External API no
        resuelve campos anidados (partner_id.partner_latitude) en una sola.
        """
        project_ids = self._project_ids(project_name)
        if not project_ids:
            logger.warning("No existe el proyecto '%s' en Odoo todavía.", project_name)
            return []

        tasks = self._execute(
            "project.task", "search_read", [["project_id", "in", project_ids]],
            ["id", "name", "partner_id", "state", "write_date", "tag_ids"],
        )
        partner_ids = list({t["partner_id"][0] for t in tasks if t.get("partner_id")})
        partners: dict[int, dict] = {}
        if partner_ids:
            rows = self._execute(
                "res.partner", "search_read", [["id", "in", partner_ids]],
                ["id", "name", "street", "city", "phone", "email",
                 "partner_latitude", "partner_longitude", "write_date"],
            )
            partners = {p["id"]: p for p in rows}

        results = []
        for t in tasks:
            p = partners.get(t["partner_id"][0]) if t.get("partner_id") else None
            p = p or {}
            results.append({
                "task_id": t["id"],
                "description": t["name"],
                "state_code": t.get("state"),
                "stage": self.state_label(t.get("state")),
                "write_date": t.get("write_date"),
                "tag_ids": t.get("tag_ids") or [],
                "partner_id": p.get("id"),
                "partner_write_date": p.get("write_date"),
                "citizen_name": p.get("name") or "",
                "street": p.get("street") or "",
                "city": p.get("city") or "",
                "phone": p.get("phone") or "",
                "email": p.get("email") or "",
                "partner_latitude": p.get("partner_latitude"),
                "partner_longitude": p.get("partner_longitude"),
            })
        return results

    def create_task_from_arcgis(self, record: dict, project_name: str) -> int:
        """
        Crea contacto + tarea a partir de una feature nueva sin vincular
        (Survey123 / Field Maps). El CONTACTO toma `citizen_name`; `name` es
        la descripción del problema y va solo al título de la tarea.
        """
        project_id = self.get_or_create_project(project_name)
        partner_id = self._execute("res.partner", "create", {
            "name": record.get("citizen_name") or "Ciudadano (reporte de campo)",
            "street": record.get("address") or "",
            "city": record.get("city") or "",
            "phone": record.get("phone") or "",
            "email": record.get("email") or "",
            "partner_latitude": record.get("_lat") or 0.0,
            "partner_longitude": record.get("_lon") or 0.0,
        })
        return self._execute("project.task", "create", {
            "name": record.get("name") or "Reporte de campo (ArcGIS)",
            "project_id": project_id,
            "partner_id": partner_id,
        })

    def update_partner_location(self, partner_id: int, lat: float, lon: float) -> None:
        self._execute("res.partner", "write", [partner_id], {
            "partner_latitude": lat, "partner_longitude": lon,
        })

    # ------------------------------------------------ frentes de trabajo --

    def get_workfront_tasks(self, project_name: str) -> list[dict]:
        project_ids = self._project_ids(project_name)
        if not project_ids:
            return []
        rows = self._execute(
            "project.task", "search_read", [["project_id", "in", project_ids]],
            ["id", "name", "state", "write_date"],
        )
        return [{
            "task_id": r["id"],
            "name": r["name"],
            "state_code": r.get("state"),
            "stage": self.state_label(r.get("state")),
            "write_date": r.get("write_date"),
        } for r in rows]

    def create_workfront_task(self, project_name: str, vals: dict[str, Any]) -> int:
        project_id = self.get_or_create_project(project_name)
        return self._execute("project.task", "create", {**vals, "project_id": project_id})

    def update_task(self, task_id: int, vals: dict[str, Any]) -> None:
        self._execute("project.task", "write", [task_id], vals)

    # ------------------------------------------------------------- notas --

    def post_note(self, task_id: int, html_body: str,
                  attachment_ids: Iterable[int] | None = None) -> int:
        """
        Nota interna en el chatter. El HTML debe venir ya escapado en las
        partes que provienen de usuarios (ver sync._esc).
        """
        kwargs: dict[str, Any] = {
            "body": html_body,
            "body_is_html": True,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_note",
        }
        if attachment_ids:
            kwargs["attachment_ids"] = list(attachment_ids)
        return self._execute("project.task", "message_post", task_id, **kwargs)

    def post_note_on_task(self, task_id: int, body: str) -> None:
        """Compatibilidad con versiones anteriores (cuerpo HTML simple)."""
        self.post_note(task_id, body)

    # ---------------------------------------------------------- etiquetas --

    def front_tags(self) -> dict[int, str]:
        """Etiquetas de frente existentes: {tag_id: nombre}."""
        rows = self._execute("project.tags", "search_read",
                             [["name", "=like", f"{FRONT_TAG_PREFIX}%"]], ["id", "name"])
        return {r["id"]: r["name"] for r in rows}

    def create_tag(self, name: str, color: int) -> int:
        return self._execute("project.tags", "create", {"name": name, "color": color})

    def rename_tag(self, tag_id: int, name: str) -> None:
        self._execute("project.tags", "write", [tag_id], {"name": name})

    def delete_tags(self, tag_ids: Iterable[int]) -> None:
        ids = list(tag_ids)
        if ids:
            self._execute("project.tags", "unlink", ids)

    def change_task_tags(self, task_id: int, add: Iterable[int] = (),
                         remove: Iterable[int] = ()) -> None:
        commands = [(4, t) for t in add] + [(3, t) for t in remove]
        if commands:
            self._execute("project.task", "write", [task_id], {"tag_ids": commands})

    # ---------------------------------------------------------- adjuntos --

    def synced_attachment_keys(self, task_ids: Iterable[int]) -> set[str]:
        """Claves 'arcgis:...' de adjuntos ya copiados (idempotencia de fotos)."""
        ids = list(task_ids)
        if not ids:
            return set()
        rows = self._execute(
            "ir.attachment", "search_read",
            [["res_model", "=", "project.task"], ["res_id", "in", ids],
             ["description", "=like", "arcgis:%"]],
            ["description"],
        )
        return {r["description"] for r in rows if r.get("description")}

    def attach_file(self, task_id: int, name: str, data: bytes,
                    mimetype: str | None, key: str) -> int:
        vals = {
            "name": name,
            "datas": base64.b64encode(data).decode("ascii"),
            "res_model": "project.task",
            "res_id": task_id,
            "description": key,
        }
        if mimetype:
            vals["mimetype"] = mimetype
        return self._execute("ir.attachment", "create", vals)

    # ---------------------------------------------------- reconciliación --

    def filter_existing_task_ids(self, task_ids: list[int]) -> set[int]:
        """Subconjunto de ids de project.task que todavía existen."""
        if not task_ids:
            return set()
        return set(self._execute("project.task", "search", [["id", "in", list(task_ids)]]))
