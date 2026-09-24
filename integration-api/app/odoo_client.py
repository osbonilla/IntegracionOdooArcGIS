"""
Cliente Odoo usando XML-RPC (External API estándar de Odoo).

Modelo de datos: cada solicitud ciudadana es una tarea (`project.task`)
dentro del proyecto ODOO_PROJECT_NAME, vinculada a un contacto
(`res.partner`) que representa al ciudadano y lleva la geolocalización
(`partner_latitude` / `partner_longitude`, del módulo `base_geolocalize`).

El estado de la solicitud se lee del campo `state` (Selection) de
project.task -- NO de `stage_id` (que es el modelo de columnas Kanban,
un concepto distinto en Odoo). `state` devuelve un código interno
(ej. "01_in_progress"), no la etiqueta visible ("In Progress"), por eso
se resuelve dinámicamente vía fields_get en vez de asumir un mapeo fijo.
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
        self._state_label_map: dict[str, str] | None = None

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

    def _execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        Envoltorio sobre execute_kw. args se envían como parámetros
        posicionales del método de Odoo; kwargs (si se pasan) se envían
        como el diccionario de kwargs de execute_kw, necesario para
        métodos con parámetros keyword-only como message_post(body=...).
        """
        uid = self.authenticate()
        models = self._models()
        if kwargs:
            return models.execute_kw(
                self.db, uid, self.password, model, method, list(args), kwargs
            )
        return models.execute_kw(
            self.db, uid, self.password, model, method, list(args)
        )

    def get_or_create_project(self, project_name: str) -> int:
        project_ids = self._execute(
            "project.project", "search", [["name", "=", project_name]]
        )
        if project_ids:
            return project_ids[0]
        return self._execute("project.project", "create", {"name": project_name})

    def _get_state_label_map(self) -> dict[str, str]:
        """
        Mapeo {código interno: etiqueta} del campo Selection `state` de
        project.task, leído dinámicamente vía fields_get -- no hardcodeado,
        para no asumir un esquema fijo si el proyecto personaliza los
        valores (ej. "01_in_progress" -> "In Progress").
        """
        if self._state_label_map is None:
            info = self._execute("project.task", "fields_get", ["state"], ["selection"])
            self._state_label_map = dict(info["state"]["selection"])
        return self._state_label_map

    def get_project_tasks(self, project_name: str) -> list[dict]:
        """
        Trae las tareas (project.task) del proyecto `project_name`, junto
        con los datos del contacto (res.partner) vinculado a cada una:
        nombre del ciudadano y geolocalización. Requiere dos consultas
        porque la External API de Odoo no resuelve campos de relaciones
        anidadas (partner_id.partner_latitude) en una sola llamada.
        """
        project_ids = self._execute(
            "project.project", "search", [["name", "=", project_name]]
        )
        if not project_ids:
            logger.warning("No existe el proyecto '%s' en Odoo todavía.", project_name)
            return []

        label_map = self._get_state_label_map()

        task_fields = ["id", "name", "partner_id", "state", "write_date"]
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
                "id", "name", "street", "city", "phone", "email",
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
                "stage": label_map.get(task.get("state"), task.get("state") or ""),
                "citizen_name": partner.get("name") or "",
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
        self._execute("project.task", "message_post", task_id, body=body)
        logger.info("Nota registrada en project.task id=%s", task_id)

    def set_task_state_by_label(self, task_id: int, label: str) -> bool:
        """
        Mueve la tarea al estado (campo `state`) cuya etiqueta coincide
        con `label` (ej. "Approved", "Done"). Devuelve False si no existe
        esa etiqueta (en cuyo caso el llamador debe decidir si igual
        registrar el cambio como nota, ver sync.handle_arcgis_status_webhook).
        """
        label_map = self._get_state_label_map()
        code = next((k for k, v in label_map.items() if v == label), None)
        if code is None:
            return False
        self._execute("project.task", "write", [task_id], {"state": code})
        return True

    def create_task_from_arcgis(self, record: dict, project_name: str) -> int:
        """
        Crea contacto + tarea en Odoo a partir de una feature nueva sin
        vincular (típicamente proveniente de Survey123 o de una edición
        manual en ArcGIS). El nombre del CONTACTO se toma de
        `citizen_name` (lo que el ciudadano escribió en "Tu nombre
        completo" en el formulario) -- `name` es la descripción del
        problema, no el nombre de la persona, y no debe usarse para el
        contacto. `record` trae _lat/_lon (de la geometría) además de
        los campos de negocio (citizen_name, name, address, city,
        phone, email).
        """
        project_id = self.get_or_create_project(project_name)
        citizen_name = record.get("citizen_name") or "Ciudadano (Survey123)"
        partner_id = self._execute("res.partner", "create", {
            "name": citizen_name,
            "street": record.get("address") or "",
            "city": record.get("city") or "",
            "phone": record.get("phone") or "",
            "email": record.get("email") or "",
            "partner_latitude": record.get("_lat"),
            "partner_longitude": record.get("_lon"),
        })
        task_id = self._execute("project.task", "create", {
            "name": record.get("name") or "Reporte vía ArcGIS",
            "project_id": project_id,
            "partner_id": partner_id,
        })
        return task_id

    def get_task_state_label(self, task_id: int) -> str:
        """
        Lee el estado (`state`) actual de una tarea y lo resuelve a su
        etiqueta visible. Se usa justo después de create_task_from_arcgis
        para poder escribir el status inicial de vuelta en ArcGIS en la
        MISMA pasada del reverse-sync, en vez de dejarlo en blanco hasta
        el próximo ciclo del sync hacia adelante (Odoo -> ArcGIS) -- sin
        esto, una feature nueva creada por Survey123 aparecía en el mapa
        pero con el status vacío hasta que corriera ese segundo ciclo.
        """
        label_map = self._get_state_label_map()
        task = self._execute("project.task", "read", [task_id], ["state"])
        code = task[0]["state"] if task else None
        return label_map.get(code, code or "")

    def filter_existing_task_ids(self, task_ids: list[int]) -> set[int]:
        """
        De una lista de ids de project.task, devuelve el subconjunto que
        SÍ existe todavía en Odoo. Se usa en la reconciliación para
        detectar referencias huérfanas en ArcGIS: features cuyo
        odoo_partner_id apunta a una tarea que ya fue borrada
        directamente en Odoo (por ejemplo, al limpiar un registro de
        prueba duplicado).
        """
        if not task_ids:
            return set()
        existing = self._execute(
            "project.task", "search", [["id", "in", list(task_ids)]]
        )
        return set(existing)