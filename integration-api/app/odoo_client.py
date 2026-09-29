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

Autoría: lo que llega de campo se firma con el usuario de ArcGIS que lo hizo
(`author_id`), no con el usuario técnico de la integración. Para eso:
- las tareas se crean con `mail_create_nolog` (sin el "Tarea creada" que
  Odoo firmaría con el usuario técnico) y la integración publica ese mensaje
  con el autor real;
- los cambios de estado y de datos del frente se escriben con `mail_notrack`
  y van acompañados de una nota firmada por quien los hizo en campo;
- message_post exige que el autor tenga correo (si no, "Unable to send
  message, please configure the sender's email address"): cuando el autor
  no tiene, se envía `email_from` con su nombre y el correo del usuario
  técnico.
"""

from __future__ import annotations

import base64
import logging
import re
import xmlrpc.client
from typing import Any, Iterable

from app.config import settings

logger = logging.getLogger("integration.odoo")

# Estados que la integración nunca escribe (Odoo los calcula).
NON_WRITABLE_STATES = {"04_waiting_normal"}
CANCELLED_STATE = "1_canceled"
FRONT_TAG_PREFIX = "Frente #"

# Contextos de escritura: sin el mensaje automático de creación y sin el
# seguimiento de cambios firmados por el usuario técnico (ver docstring).
NO_CREATION_LOG = {"context": {"mail_create_nolog": True}}
NO_TRACKING = {"context": {"mail_notrack": True}}

SYSTEM_AUTHOR_REF = "arcgis:integracion"
SYSTEM_AUTHOR_NAME = "Integración ArcGIS"
_EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")


def _like_literal(value: str) -> str:
    """Valor para =ilike sin comodines (los usuarios de ArcGIS suelen llevar '_')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class OdooClient:
    def __init__(self) -> None:
        self.url = settings.odoo_url
        self.db = settings.odoo_db
        self.username = settings.odoo_username
        self.password = settings.odoo_password
        self._uid: int | None = None
        self._state_label_map: dict[str, str] | None = None
        self._own_email: str | None = None

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
        # Sin seguimiento: el cambio lo firma la nota de quien lo hizo en campo.
        self._execute("project.task", "write", [task_id], {"state": code}, **NO_TRACKING)

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

    def create_task_from_arcgis(self, record: dict, project_name: str,
                                contact_name: str | None = None) -> tuple[int, dict]:
        """
        Crea contacto + tarea a partir de una feature nueva sin vincular
        (Survey123 / Field Maps). El CONTACTO toma `citizen_name` (o
        `contact_name`, p. ej. el usuario de Field Maps que lo reportó); `name`
        es la descripción del problema y va solo al título de la tarea.
        Devuelve (task_id, contacto como autor) para firmar la creación.
        """
        project_id = self.get_or_create_project(project_name)
        name = record.get("citizen_name") or contact_name or "Ciudadano (reporte de campo)"
        email = record.get("email") or ""
        partner_id = self._execute("res.partner", "create", {
            "name": name,
            "street": record.get("address") or "",
            "city": record.get("city") or "",
            "phone": record.get("phone") or "",
            "email": email,
            "partner_latitude": record.get("_lat") or 0.0,
            "partner_longitude": record.get("_lon") or 0.0,
        }, **NO_CREATION_LOG)
        task_id = self._execute("project.task", "create", {
            "name": record.get("name") or "Reporte de campo (ArcGIS)",
            "project_id": project_id,
            "partner_id": partner_id,
        }, **NO_CREATION_LOG)
        return task_id, {"partner_id": partner_id, "name": name, "email": email or None}

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
        return self._execute("project.task", "create", {**vals, "project_id": project_id},
                             **NO_CREATION_LOG)

    def update_task(self, task_id: int, vals: dict[str, Any], tracking: bool = True) -> None:
        if tracking:
            self._execute("project.task", "write", [task_id], vals)
        else:
            self._execute("project.task", "write", [task_id], vals, **NO_TRACKING)

    # ------------------------------------------------------------- autores --

    def _own_sender_email(self) -> str:
        """Correo del usuario técnico: remitente cuando el autor no tiene correo."""
        if self._own_email is None:
            rows = self._execute("res.users", "read", [self.authenticate()], ["email"])
            email = (rows[0].get("email") or "") if rows else ""
            self._own_email = email if _EMAIL_RE.match(email) else "noreply@localhost"
        return self._own_email

    def _email_from(self, author: dict) -> str:
        name = str(author.get("name") or "").replace('"', "'").replace("\\", "")
        email = author.get("email") or ""
        if not _EMAIL_RE.match(email):
            email = self._own_sender_email()
        return f'"{name}" <{email}>'

    def _contact_by_ref(self, ref: str, name: str, email: str | None = None) -> dict:
        """Contacto identificado por `ref`; se crea la primera vez. Si alguien lo
        renombra en Odoo (p. ej. con el nombre completo), se respeta."""
        rows = self._execute("res.partner", "search_read", [["ref", "=", ref]],
                             ["id", "name", "email"], limit=1, order="id asc",
                             context={"active_test": False})
        if rows:
            r = rows[0]
            return {"partner_id": r["id"], "name": r["name"], "email": r.get("email") or None}
        partner_id = self._execute("res.partner", "create", {
            "name": name, "ref": ref, "email": email or False,
            "comment": "<p>Usuario de ArcGIS (Field Maps / Survey123). Lo creó la "
                       "integración para firmar en Odoo lo que este usuario hace en campo.</p>",
        }, **NO_CREATION_LOG)
        return {"partner_id": partner_id, "name": name, "email": email}

    def author_for_arcgis_user(self, username: str, full_name: str | None = None,
                               email: str | None = None, odoo_login: str | None = None) -> dict:
        """
        Autor en Odoo de lo que hizo un usuario de ArcGIS en campo:
        1. el usuario interno de Odoo indicado en ARCGIS_ODOO_USERS, o cuyo
           login o correo coincide con el usuario / correo de ArcGIS;
        2. si no hay, un contacto "arcgis:<usuario>" con su nombre completo (si
           ArcGIS lo entrega) o su nombre de usuario.
        """
        keys = [k for k in dict.fromkeys((odoo_login, username, email)) if k]
        conditions = [[field, "=ilike", _like_literal(k)] for k in keys for field in ("login", "email")]
        domain = ["&", ["share", "=", False]] + ["|"] * (len(conditions) - 1) + conditions
        users = self._execute("res.users", "search_read", domain,
                              ["partner_id", "name", "email"], limit=1, order="id asc")
        if users:
            u = users[0]
            return {"partner_id": u["partner_id"][0], "name": u["name"], "email": u.get("email") or None}
        return self._contact_by_ref(f"arcgis:{username}", full_name or username, email)

    def system_author(self) -> dict:
        """Autor de las notas que genera la propia integración (asignaciones, ajustes)."""
        return self._contact_by_ref(SYSTEM_AUTHOR_REF, SYSTEM_AUTHOR_NAME)

    # ------------------------------------------------------------- notas --

    def _post(self, task_id: int, author: dict | None, **kwargs: Any) -> int:
        if author:
            kwargs["author_id"] = author["partner_id"]
            kwargs["email_from"] = self._email_from(author)
        return self._execute("project.task", "message_post", task_id,
                             body_is_html=True, **kwargs)

    def post_note(self, task_id: int, html_body: str,
                  attachment_ids: Iterable[int] | None = None,
                  author: dict | None = None) -> int:
        """
        Nota interna en el chatter, firmada por `author` (sin autor: el
        usuario técnico). El HTML debe venir ya escapado en las partes que
        provienen de usuarios (ver sync._esc).
        """
        kwargs: dict[str, Any] = {"body": html_body, "message_type": "comment",
                                  "subtype_xmlid": "mail.mt_note"}
        if attachment_ids:
            kwargs["attachment_ids"] = list(attachment_ids)
        return self._post(task_id, author, **kwargs)

    def post_creation(self, task_id: int, html_body: str, author: dict | None = None) -> int:
        """El "Tarea creada" que Odoo pondría solo, pero firmado por quien la creó en campo."""
        return self._post(task_id, author, body=html_body, message_type="notification",
                          subtype_xmlid="project.mt_task_new")

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