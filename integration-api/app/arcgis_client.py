"""
Cliente ArcGIS usando "ArcGIS API for Python" (paquete `arcgis`).
Funciona igual para ArcGIS Online y ArcGIS Enterprise: lo único que
cambia es ARCGIS_URL y, según la cuenta, el método de autenticación
(ver connect()).
"""

import logging
from typing import Any

from arcgis.gis import GIS
from arcgis.features import FeatureLayer

from app.config import settings

logger = logging.getLogger("integration.arcgis")

# Nombre del campo en la Feature Layer que guarda el identificador único
# del registro de origen en Odoo (el id de la tarea project.task). Se
# conserva el nombre histórico `odoo_partner_id` por compatibilidad con
# capas publicadas en iteraciones previas de este proyecto (cuando el
# origen era res.partner).
ODOO_ID_FIELD = "odoo_partner_id"

# Valor centinela usado para "reclamar" una feature antes de crear la
# tarea correspondiente en Odoo (ver claim_feature). Ningún project.task
# real va a tener nunca id=-1 en Odoo (los ids son autoincrementales
# positivos), así que es seguro usarlo como marca de "procesando" sin
# confundirse con un odoo_partner_id real.
CLAIM_SENTINEL = -1


class ArcGISClient:
    def __init__(self) -> None:
        self._gis: GIS | None = None
        self._layer: FeatureLayer | None = None
        self._layer_fields: set[str] | None = None

    def connect(self) -> GIS:
        if self._gis is None:
            if settings.arcgis_client_id and settings.arcgis_client_secret:
                # App Authentication (OAuth 2.0 client credentials): la
                # aplicación se autentica a sí misma, no un usuario -> no
                # se ve afectada por MFA. Recomendado para servicios
                # desatendidos como este. Requiere que las credenciales
                # OAuth tengan acceso concedido al item de la capa
                # (ver README sección 5.3).
                self._gis = GIS(
                    settings.arcgis_url,
                    client_id=settings.arcgis_client_id,
                    client_secret=settings.arcgis_client_secret,
                    verify_cert=settings.arcgis_verify_cert,
                )
                logger.info(
                    "Conectado a ArcGIS vía App Authentication (%s)",
                    settings.arcgis_url,
                )
            else:
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
                "ARCGIS_FEATURE_LAYER_ITEM_ID no está configurado. Ver README "
                "sección 5.2 para crear/publicar la capa."
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
        """
        Nombre real del campo ObjectID de la capa. Varía según cómo se
        publicó (CSV -> típicamente 'objectid'; create_service() vía
        script -> 'OBJECTID') — nunca se asume un nombre fijo.
        """
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
        # Solo se completan si existen en el esquema real de la capa -
        # evita asumir un esquema fijo (ver README sección 9).
        if "latitude" in field_names:
            attributes["latitude"] = lat
        if "longitude" in field_names:
            attributes["longitude"] = lon
        if "citizen_name" in field_names:
            attributes["citizen_name"] = record.get("citizen_name") or ""

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

    def get_unlinked_features(self) -> list[dict]:
        """
        Features sin odoo_partner_id: creadas directo en la capa (edición
        manual) o vía un formulario Survey123 apuntado a esta misma capa.
        Son las candidatas a convertirse en tareas nuevas en Odoo. No
        incluye las que tienen el centinela de claim (CLAIM_SENTINEL):
        esas ya están siendo procesadas por otra corrida.

        out_sr=4326 fuerza a que la geometría vuelva en grados WGS84,
        sin importar la referencia espacial de almacenamiento nativa de
        la capa (la mayoría de las Hosted Feature Layers de AGOL
        guardan internamente en Web Mercator/3857, aunque se vea todo
        en grados en el mapa). Sin esto, f.geometry["x"]/["y"] venían
        en metros Web Mercator, no en grados -- por eso las coordenadas
        quedaban con valores absurdos (decenas de miles / millones) al
        crear la tarea en Odoo desde una feature de Survey123.
        """
        layer = self.get_layer()
        result = layer.query(where=f"{ODOO_ID_FIELD} IS NULL", out_sr=4326)
        features = []
        for f in result.features:
            attrs = dict(f.attributes)
            attrs["_oid"] = attrs[self._oid_field()]
            attrs["_lat"] = f.geometry["y"] if f.geometry else None
            attrs["_lon"] = f.geometry["x"] if f.geometry else None
            features.append(attrs)
        return features

    def set_odoo_id(self, object_id: int, odoo_task_id: int) -> None:
        """
        Escribe de vuelta el task_id de Odoo en la feature. Se mantiene
        por compatibilidad, pero el flujo de reverse-sync usa
        link_new_feature() en su lugar, que además fija el status
        inicial en la misma llamada.
        """
        layer = self.get_layer()
        layer.edit_features(updates=[{
            "attributes": {self._oid_field(): object_id, ODOO_ID_FIELD: odoo_task_id}
        }])

    def link_new_feature(self, object_id: int, odoo_task_id: int, status_label: str) -> None:
        """
        Cierra el ciclo de una feature nueva (creada por Survey123 o
        edición manual): escribe en UNA sola llamada el odoo_partner_id
        (vínculo) Y el status inicial de la tarea recién creada en Odoo.
        Reemplaza a set_odoo_id() en el flujo de reverse-sync -- con
        solo set_odoo_id() el status quedaba vacío hasta el próximo
        ciclo del sync hacia adelante (Odoo -> ArcGIS).
        """
        layer = self.get_layer()
        attributes: dict[str, Any] = {
            self._oid_field(): object_id,
            ODOO_ID_FIELD: odoo_task_id,
        }
        if "status" in self._layer_field_names():
            attributes["status"] = status_label
        layer.edit_features(updates=[{"attributes": attributes}])

    def claim_feature(self, object_id: int) -> bool:
        """
        Reclama una feature ANTES de crear la tarea en Odoo, escribiendo
        el valor centinela en odoo_partner_id. Esto es lo que hace
        idempotente al reverse-sync: si dos corridas se solapan (el
        scheduler y un curl manual al mismo tiempo, por ejemplo), la
        segunda ya no va a ver esta feature en get_unlinked_features()
        (que filtra por odoo_partner_id IS NULL) porque ya dejó de ser
        NULL -- sin este paso, ambas corridas podrían crear una tarea
        duplicada en Odoo para la misma feature.
        """
        layer = self.get_layer()
        result = layer.edit_features(updates=[{
            "attributes": {self._oid_field(): object_id, ODOO_ID_FIELD: CLAIM_SENTINEL}
        }])
        return result["updateResults"][0]["success"]

    def release_claim(self, object_id: int) -> None:
        """
        Revierte un claim si la creación en Odoo falló después de
        reclamar la feature -- la deja de nuevo en NULL para que la
        próxima corrida la vuelva a intentar, en vez de quedar huérfana
        con el centinela para siempre.
        """
        layer = self.get_layer()
        layer.edit_features(updates=[{
            "attributes": {self._oid_field(): object_id, ODOO_ID_FIELD: None}
        }])

    def get_linked_features(self) -> list[dict]:
        """
        Features CON odoo_partner_id asignado (excluyendo el centinela
        de claim, que representa un procesamiento en curso, no un
        vínculo real todavía). Se usa en la reconciliación: por cada
        una se verifica si esa tarea sigue existiendo en Odoo.
        """
        layer = self.get_layer()
        oid_field = self._oid_field()
        result = layer.query(
            where=f"{ODOO_ID_FIELD} IS NOT NULL AND {ODOO_ID_FIELD} <> {CLAIM_SENTINEL}",
            out_fields=[oid_field, ODOO_ID_FIELD],
            return_geometry=False,
        )
        return [
            {"_oid": f.attributes[oid_field], "task_id": f.attributes[ODOO_ID_FIELD]}
            for f in result.features
        ]

    def delete_features(self, object_ids: list[int]) -> int:
        """
        Borra features por ObjectID. Se usa en la reconciliación para
        eliminar de la capa las features cuya tarea de Odoo ya no existe
        (fue borrada directamente en Odoo) -- lo que hay en Odoo manda;
        ArcGIS debe reflejar ese mismo conjunto, sin huérfanos. Devuelve
        cuántas se borraron con éxito.
        """
        if not object_ids:
            return 0
        layer = self.get_layer()
        result = layer.edit_features(deletes=object_ids)
        return sum(1 for r in result.get("deleteResults", []) if r.get("success"))