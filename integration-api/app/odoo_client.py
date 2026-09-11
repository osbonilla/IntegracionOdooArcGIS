"""
Cliente Odoo usando XML-RPC (External API estándar de Odoo).
"""

import logging
import xmlrpc.client
from typing import Any

from app.config import settings

logger = logging.getLogger("integration.odoo")


class OdooClient:
    def __init__(self) -> None:
        self.url = settings.odoo_url
        self.db = settings.odoo_db
        self.username = settings.odoo_username
        self.password = settings.odoo_password
        self._uid: int | None = None

    def _common(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")

    def _models(self):
        return xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def authenticate(self) -> int:
        if self._uid:
            return self._uid
        common = self._common()
        uid = common.authenticate(self.db, self.username, self.password, {})
        if not uid:
            raise RuntimeError(
                "Autenticación con Odoo falló. Revisa ODOO_DB / ODOO_USERNAME / "
                "ODOO_PASSWORD en integration-api/.env"
            )
        self._uid = uid
        logger.info("Autenticado en Odoo como uid=%s (db=%s)", uid, self.db)
        return uid

    def _execute(self, model: str, method: str, *args: Any) -> Any:
        uid = self.authenticate()
        models = self._models()
        return models.execute_kw(
            self.db, uid, self.password, model, method, list(args)
        )

    def get_project_tasks(self, project_name: str) -> list[dict]:
        """
        Trae las tareas (project.task) del proyecto `project_name`, junto
        con los datos de geolocalización del contacto (res.partner)
        vinculado a cada tarea. Requiere dos llamadas porque la External
        API de Odoo no resuelve campos de relaciones anidadas
        (partner_id.partner_latitude) en una sola consulta.
        """
        project_ids = self._execute(
            "project.project", "search", [["name", "=", project_name]]
        )
        if not project_ids:
            logger.warning("No existe el proyecto '%s' en Odoo todavía.", project_name)
            return []

        task_fields = ["id", "name", "partner_id", "stage_id", "write_date"]
        tasks = self._execute(
            "project.task", "search_read",
            [["project_id", "in", project_ids]], task_fields
        )
        if not tasks:
            return []

        partner_ids = list({t["partner_id"][0] for t in tasks if t.get("partner_id")})
        partners_by_id: dict[int, dict] = {}
        if partner_ids:
            partner_fields = [
                "id", "street", "city", "phone", "email",
                "partner_latitude", "partner_longitude",
            ]
            partners = self._execute(
                "res.partner", "search_read", [["id", "in", partner_ids]], partner_fields
            )
            partners_by_id = {p["id"]: p for p in partners}

        results = []
        for task in tasks:
            partner = partners_by_id.get(task["partner_id"][0]) if task.get("partner_id") else {}
            results.append({
                "task_id": task["id"],
                "description": task["name"],
                "stage": task["stage_id"][1] if task.get("stage_id") else "",
                "street": partner.get("street") or "",
                "city": partner.get("city") or "",
                "phone": partner.get("phone") or "",
                "email": partner.get("email") or "",
                "partner_latitude": partner.get("partner_latitude"),
                "partner_longitude": partner.get("partner_longitude"),
            })
        return results

    def post_note_on_task(self, task_id: int, body: str) -> None:
        """Escribe una nota en el chatter de la tarea (flujo inverso ArcGIS -> Odoo)."""
        self._execute("project.task", "message_post", task_id, {"body": body})
        logger.info("Nota registrada en project.task id=%s", task_id)