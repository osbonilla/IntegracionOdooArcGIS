"""
Cliente ArcGIS usando "ArcGIS API for Python" (paquete `arcgis`).
Funciona igual para ArcGIS Online y ArcGIS Enterprise; solo cambia la
URL de conexión (ARCGIS_URL en .env).
"""

import logging
from typing import Any

from arcgis.gis import GIS
from arcgis.features import FeatureLayer

from app.config import settings

logger = logging.getLogger("integration.arcgis")

# Nombre del campo en la Feature Layer que guarda el identificador único
# del registro de origen en Odoo. Se conserva el nombre histórico
# `odoo_partner_id` por compatibilidad con la capa ya publicada durante
# las pruebas previas (cuando el origen era res.partner); a partir de
# esta versión el valor que contiene es el ID de la tarea (project.task).
ODOO_ID_FIELD = "odoo_partner_id"


class ArcGISClient:
    def __init__(self) -> None:
        self._gis: GIS | None = None
        self._layer: FeatureLayer | None = None
        self._layer_fields: set[str] | None = None

    def connect(self) -> GIS:
        if self._gis is None:
            self._gis = GIS(
                settings.arcgis_url,
                settings.arcgis_username,
                settings.arcgis_password,
                verify_cert=settings.arcgis_verify_cert,
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
                "ARCGIS_FEATURE_LAYER_ITEM_ID no está configurado."
            )

        gis = self.connect()
        item = gis.content.get(settings.arcgis_feature_layer_item_id)
        if item is None:
            raise RuntimeError(
                f"No se encontró el item {settings.arcgis_feature_layer_item_id} en ArcGIS."
            )
        self._layer = item.layers[0]
        return self._layer

    def _oid_field(self) -> str:
        layer = self.get_layer()
        return layer.properties.objectIdField

    def _layer_field_names(self) -> set[str]:
        if self._layer_fields is None:
            layer = self.get_layer()
            self._layer_fields = {f["name"] for f in layer.properties.fields}
        return self._layer_fields

    def _find_existing_object_id(self, odoo_id: int) -> int | None:
        layer = self.get_layer()
        oid_field = self._oid_field()
        result = layer.query(
            where=f"{ODOO_ID_FIELD} = {odoo_id}",
            out_fields=[oid_field],
            return_geometry=False,
        )
        if result.features:
            return result.features[0].attributes[oid_field]
        return None

    def upsert_request(self, record: dict[str, Any]) -> str:
        """
        Inserta o actualiza una feature a partir de un dict combinado
        tarea + contacto (ver OdooClient.get_project_tasks). Devuelve
        "created", "updated" o "skipped" (sin coordenadas).
        """
        lat = record.get("partner_latitude")
        lon = record.get("partner_longitude")
        if not lat or not lon:
            return "skipped"

        layer = self.get_layer()
        field_names = self._layer_field_names()

        attributes: dict[str, Any] = {
            ODOO_ID_FIELD: record["task_id"],
            "name": record.get("description") or "",
            "address": record.get("street") or "",
            "city": record.get("city") or "",
            "phone": record.get("phone") or "",
            "email": record.get("email") or "",
            "status": record.get("stage") or "",
        }
        if "latitude" in field_names:
            attributes["latitude"] = lat
        if "longitude" in field_names:
            attributes["longitude"] = lon

        geometry = {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}

        existing_oid = self._find_existing_object_id(record["task_id"])
        if existing_oid is not None:
            attributes[self._oid_field()] = existing_oid
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