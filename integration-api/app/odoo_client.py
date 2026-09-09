"""
Cliente Odoo usando XML-RPC (External API estándar de Odoo).

Se usa XML-RPC en lugar de una librería tipo odoorpc para minimizar
dependencias: funciona contra cualquier versión de Odoo (13 a 18+)
sin cambios, y es el mecanismo soportado oficialmente por Odoo para
integraciones de terceros.

Docs oficiales: https://www.odoo.com/documentation/18.0/developer/reference/external_api.html
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

    def get_tagged_partners(self, tag_name: str) -> list[dict]:
        """
        Trae contactos (res.partner) marcados con el tag `tag_name`
        (res.partner.category), que tengan coordenadas cargadas.
        Estas coordenadas nativas de Odoo (partner_latitude / partner_longitude)
        vienen del módulo base_geolocalize, incluido en Odoo Community.
        """
        category_ids = self._execute(
            "res.partner.category", "search", [["name", "=", tag_name]]
        )
        if not category_ids:
            logger.warning("No existe el tag '%s' en Odoo todavía.", tag_name)
            return []

        domain = [["category_id", "in", category_ids]]
        fields = [
            "id",
            "name",
            "street",
            "city",
            "phone",
            "email",
            "partner_latitude",
            "partner_longitude",
            "write_date",
        ]
        partners = self._execute(
            "res.partner", "search_read", domain, fields
        )
        return partners

    def post_note(self, partner_id: int, body: str) -> None:
        """
        Escribe una nota en el chatter del contacto (message_post).
        Se usa para el flujo inverso ArcGIS -> Odoo (trazabilidad).
        """
        self._execute("res.partner", "message_post", partner_id, {"body": body})
        logger.info("Nota registrada en res.partner id=%s", partner_id)

    def ensure_tag(self, tag_name: str) -> int:
        """Crea el tag si no existe. Usado por scripts/seed_odoo_demo_data.py."""
        category_ids = self._execute(
            "res.partner.category", "search", [["name", "=", tag_name]]
        )
        if category_ids:
            return category_ids[0]
        return self._execute("res.partner.category", "create", {"name": tag_name})

    def create_partner(self, values: dict) -> int:
        return self._execute("res.partner", "create", values)
