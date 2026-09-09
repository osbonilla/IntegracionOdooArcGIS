"""
Cliente ArcGIS usando "ArcGIS API for Python" (paquete `arcgis`).

Funciona igual para ArcGIS Online y ArcGIS Enterprise: lo único que cambia
es la URL de conexión (ARCGIS_URL en .env):
  - AGOL:       https://www.arcgis.com
  - Enterprise: https://<host>/<portal-web-adaptor>   (ej: https://gis.municipio.gob.ec/portal)

Requiere autenticación básica (usuario/password) en esta demo. En producción
se recomienda:
  - AGOL:       OAuth 2.0 (App Registration + client_id/client_secret)
  - Enterprise: OAuth 2.0 o certificados PKI / IWA según la configuración del Portal
"""

import logging
from typing import Any

from arcgis.gis import GIS
from arcgis.features import FeatureLayer

from app.config import settings

logger = logging.getLogger("integration.arcgis")

# Nombre del campo en la Feature Layer que guarda el ID del contacto en Odoo.
# Debe existir y ser único (ver scripts/create_arcgis_feature_layer.py).
ODOO_ID_FIELD = "odoo_partner_id"


class ArcGISClient:
    def __init__(self) -> None:
        self._gis: GIS | None = None
        self._layer: FeatureLayer | None = None

    def connect(self) -> GIS:
        if self._gis is None:
            self._gis = GIS(
                settings.arcgis_url,
                settings.arcgis_username,
                settings.arcgis_password,
            )
            logger.info(
                "Conectado a ArcGIS como %s (%s)",
                self._gis.users.me.username,
                settings.arcgis_url,
            )
        return self._gis

    def get_layer(self) -> FeatureLayer:
        if self._layer is not None:
            return self._layer

        if not settings.arcgis_feature_layer_item_id:
            raise RuntimeError(
                "ARCGIS_FEATURE_LAYER_ITEM_ID no está configurado. "
                "Corre scripts/create_arcgis_feature_layer.py primero."
            )

        gis = self.connect()
        item = gis.content.get(settings.arcgis_feature_layer_item_id)
        if item is None:
            raise RuntimeError(
                f"No se encontró el item {settings.arcgis_feature_layer_item_id} en ArcGIS."
            )
        self._layer = item.layers[0]
        return self._layer

    def _find_existing_object_id(self, odoo_id: int) -> int | None:
        layer = self.get_layer()
        result = layer.query(
            where=f"{ODOO_ID_FIELD} = {odoo_id}",
            out_fields=["OBJECTID"],
            return_geometry=False,
        )
        if result.features:
            return result.features[0].attributes["OBJECTID"]
        return None

    def upsert_partner(self, partner: dict[str, Any]) -> str:
        """
        Inserta o actualiza una feature a partir de un dict de res.partner
        (ver OdooClient.get_tagged_partners). Devuelve "created", "updated"
        o "skipped" (sin coordenadas).
        """
        lat = partner.get("partner_latitude")
        lon = partner.get("partner_longitude")
        if not lat or not lon:
            return "skipped"

        layer = self.get_layer()
        attributes = {
            ODOO_ID_FIELD: partner["id"],
            "name": partner.get("name") or "",
            "address": partner.get("street") or "",
            "city": partner.get("city") or "",
            "phone": partner.get("phone") or "",
            "email": partner.get("email") or "",
            "status": "Nuevo",
            "last_sync": None,  # ArcGIS lo completa como epoch si se requiere
        }
        geometry = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}

        existing_oid = self._find_existing_object_id(partner["id"])
        if existing_oid is not None:
            attributes["OBJECTID"] = existing_oid
            result = layer.edit_features(
                updates=[{"attributes": attributes, "geometry": geometry}]
            )
            ok = result["updateResults"][0]["success"]
            if not ok:
                raise RuntimeError(f"Error actualizando feature: {result}")
            return "updated"
        else:
            result = layer.edit_features(
                adds=[{"attributes": attributes, "geometry": geometry}]
            )
            ok = result["addResults"][0]["success"]
            if not ok:
                raise RuntimeError(f"Error creando feature: {result}")
            return "created"

    def get_feature_by_odoo_id(self, odoo_id: int) -> dict[str, Any] | None:
        layer = self.get_layer()
        result = layer.query(where=f"{ODOO_ID_FIELD} = {odoo_id}")
        if result.features:
            return result.features[0].attributes
        return None
