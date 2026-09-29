"""
Cliente ArcGIS (ArcGIS API for Python, paquete `arcgis`) para VARIAS capas.

La integración trabaja con dos capas:

- "solicitudes": puntos (Survey123 / Field Maps), enlazados a project.task
  por el campo histórico `odoo_partner_id`.
- "frentes": líneas dibujadas en Field Maps, enlazadas a project.task por
  `odoo_task_id`.

Cada capa es una instancia de `ArcGISLayer`; todas comparten una sola
conexión (`get_gis`). Funciona igual con ArcGIS Online y Enterprise: solo
cambia ARCGIS_URL y el método de autenticación.

Reglas que se mantienen en todo el archivo:
- Nunca se asume un esquema fijo: ObjectID, campos de editor tracking y
  campos opcionales se descubren leyendo `layer.properties`.
- Toda consulta pide `out_sr=4326`: la geometría vuelve en grados WGS84
  aunque la capa almacene en Web Mercator.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

from app.config import settings

logger = logging.getLogger("integration.arcgis")

# Nombre histórico del campo de enlace de la capa de puntos (guarda el id
# de project.task; el nombre viene de la primera versión, con res.partner).
ODOO_ID_FIELD = "odoo_partner_id"

# Valor centinela para "reclamar" una feature mientras se crea su tarea en
# Odoo. Ningún id real de Odoo es negativo.
CLAIM_SENTINEL = -1

_BATCH = 200
_gis = None

# Resultado de coerce_value cuando el valor no se puede guardar en el campo
# (p. ej. "+593 99..." en un campo entero): no se escribe.
SKIP = object()
_INT_RANGES = {
    "esriFieldTypeSmallInteger": (-32768, 32767),
    "esriFieldTypeInteger": (-2147483648, 2147483647),
    "esriFieldTypeBigInteger": (-(2 ** 63), 2 ** 63 - 1),
}
_FLOAT_TYPES = {"esriFieldTypeDouble", "esriFieldTypeSingle"}


def coerce_value(info: dict | None, value: Any) -> Any:
    """
    Convierte `value` a como lo guardaría ArcGIS en ese campo. Comparar
    contra el valor ya convertido es lo que evita escrituras eternas cuando
    el tipo de la capa no coincide con el de Odoo: por ejemplo, un teléfono
    "0999999999" (texto en Odoo) en un campo ENTERO se guarda como
    999999999; comparado tal cual, parecería distinto en cada pasada.
    """
    if info is None:
        return SKIP
    if value is None:
        return None
    ftype = info.get("type")
    if ftype in _INT_RANGES:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError, OverflowError):
            return SKIP
        low, high = _INT_RANGES[ftype]
        return number if low <= number <= high else SKIP
    if ftype in _FLOAT_TYPES:
        if isinstance(value, str) and not value.strip():
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return SKIP
    if ftype == "esriFieldTypeString":
        text = value if isinstance(value, str) else str(value)
        length = info.get("length")
        return text[:length] if length else text
    return value


def get_gis():
    """Conexión única a ArcGIS, compartida por todas las capas."""
    global _gis
    if _gis is None:
        from arcgis.gis import GIS  # import diferido: arranque más rápido

        if settings.arcgis_client_id and settings.arcgis_client_secret:
            # App Authentication (OAuth 2.0 client credentials): no depende
            # de un usuario humano, así que no le afecta el MFA.
            _gis = GIS(
                settings.arcgis_url,
                client_id=settings.arcgis_client_id,
                client_secret=settings.arcgis_client_secret,
                verify_cert=settings.arcgis_verify_cert,
            )
            logger.info("Conectado a ArcGIS vía App Authentication (%s)", settings.arcgis_url)
        else:
            _gis = GIS(
                settings.arcgis_url,
                settings.arcgis_username,
                settings.arcgis_password,
                verify_cert=settings.arcgis_verify_cert,
            )
            logger.info("Conectado a ArcGIS como %s (%s)",
                        _gis.users.me.username, settings.arcgis_url)
    return _gis


def _chunks(items: list, size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ArcGISLayer:
    def __init__(self, key: str, label: str, item_id: str | None,
                 id_field: str, layer_index: int = 0) -> None:
        self.key = key
        self.label = label
        self.item_id = item_id
        self.id_field = id_field
        self.layer_index = layer_index
        self._layer = None
        self._fields: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------ esquema --

    @property
    def configured(self) -> bool:
        return bool(self.item_id)

    def get_layer(self):
        if self._layer is None:
            if not self.item_id:
                raise RuntimeError(
                    f"La capa '{self.key}' no tiene item id configurado "
                    f"(ver README, sección de variables de entorno)."
                )
            item = get_gis().content.get(self.item_id)
            if item is None:
                raise RuntimeError(f"No se encontró el item {self.item_id} en ArcGIS.")
            self._layer = item.layers[self.layer_index]
        return self._layer

    @property
    def url(self) -> str:
        return self.get_layer().url

    def oid_field(self) -> str:
        return self.get_layer().properties.objectIdField

    def _field_defs(self) -> dict[str, dict[str, Any]]:
        if self._fields is None:
            self._fields = {f["name"]: {"type": f.get("type"), "length": f.get("length")}
                            for f in self.get_layer().properties.fields}
        return self._fields

    def field_names(self) -> set[str]:
        return set(self._field_defs())

    def field_info(self, name: str) -> dict[str, Any] | None:
        """Tipo y largo reales del campo en la capa (None si no existe)."""
        return self._field_defs().get(name)

    def has_field(self, name: str) -> bool:
        return name in self.field_names()

    def has_attachments(self) -> bool:
        return bool(self.get_layer().properties.get("hasAttachments"))

    def edit_fields(self) -> dict[str, str | None]:
        """Campos de editor tracking reales de la capa (varían entre AGOL y Enterprise)."""
        info = self.get_layer().properties.get("editFieldsInfo") or {}
        return {
            "edit_date": info.get("editDateField"),
            "editor": info.get("editorField"),
            "creator": info.get("creatorField"),
        }

    # ----------------------------------------------------------- lectura --

    def _normalize(self, feature) -> dict[str, Any]:
        attrs = dict(feature.attributes)
        attrs["_oid"] = attrs[self.oid_field()]
        geom = dict(feature.geometry) if feature.geometry else None
        attrs["_geom"] = geom
        if geom and "x" in geom and "y" in geom:
            attrs["_lat"], attrs["_lon"] = geom["y"], geom["x"]
        else:
            attrs["_lat"] = attrs["_lon"] = None
        return attrs

    def query(self, where: str = "1=1", out_fields: str | list[str] = "*",
              return_geometry: bool = True) -> list[dict[str, Any]]:
        result = self.get_layer().query(
            where=where, out_fields=out_fields,
            return_geometry=return_geometry, out_sr=4326,
        )
        return [self._normalize(f) for f in result.features]

    def unlinked_features(self) -> list[dict[str, Any]]:
        """Features sin vínculo a Odoo (creadas en Survey123 / Field Maps / a mano)."""
        return self.query(f"{self.id_field} IS NULL")

    def linked_features(self) -> list[dict[str, Any]]:
        """Features vinculadas a una tarea (excluye las reclamadas en proceso)."""
        return self.query(
            f"{self.id_field} IS NOT NULL AND {self.id_field} <> {CLAIM_SENTINEL}"
        )

    def query_near(self, geometry: dict, distance_m: float,
                   where: str = "1=1") -> list[dict[str, Any]]:
        """
        Features a <= distance_m de una polilínea (WGS84), resuelto por el
        servidor de ArcGIS con los parámetros `distance`/`units` de la
        consulta. Se usa como filtro de candidatos: la distancia exacta la
        calcula luego geo.point_to_polyline_m.
        """
        geometry_filter = {
            "geometry": {"paths": geometry.get("paths", []),
                         "spatialReference": {"wkid": 4326}},
            "geometryType": "esriGeometryPolyline",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
        }
        result = self.get_layer().query(
            where=where, out_fields=[self.oid_field(), self.id_field],
            geometry_filter=geometry_filter, distance=distance_m,
            units="esriSRUnit_Meter", return_geometry=True, out_sr=4326,
        )
        return [self._normalize(f) for f in result.features]

    # ---------------------------------------------------------- escritura --

    def claim_feature(self, object_id: int) -> bool:
        """
        Marca la feature con el centinela ANTES de crear su tarea en Odoo:
        una corrida solapada ya no la ve como "sin vincular" (idempotencia).
        """
        result = self.get_layer().edit_features(updates=[{
            "attributes": {self.oid_field(): object_id, self.id_field: CLAIM_SENTINEL}
        }])
        return result["updateResults"][0]["success"]

    def release_claim(self, object_id: int) -> None:
        """Revierte el claim si falló la creación en Odoo (se reintenta luego)."""
        self.get_layer().edit_features(updates=[{
            "attributes": {self.oid_field(): object_id, self.id_field: None}
        }])

    def _existing_only(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """Solo campos que existen, con el valor convertido al tipo del campo."""
        out = {}
        for k, v in attributes.items():
            coerced = coerce_value(self.field_info(k), v)
            if coerced is not SKIP:
                out[k] = coerced
        return out

    def update_features(self, updates: list[dict[str, Any]]) -> tuple[int, list[str]]:
        """
        updates: [{"oid": int, "attributes": {...}, "geometry": {...} | None}].
        Los atributos que la capa no tiene se descartan en silencio (esquema
        tolerante). Devuelve (cantidad OK, errores).
        """
        if not updates:
            return 0, []
        oid_field = self.oid_field()
        payload = []
        for u in updates:
            attrs = self._existing_only(u.get("attributes", {}))
            attrs[oid_field] = u["oid"]
            item: dict[str, Any] = {"attributes": attrs}
            if u.get("geometry"):
                item["geometry"] = u["geometry"]
            payload.append(item)
        # Qué se escribe y en qué feature: si la misma feature y el mismo
        # campo aparecen en cada pasada, hay una diferencia que no converge.
        detail = [f"oid={p['attributes'][oid_field]}: "
                  + ", ".join(sorted(k for k in p["attributes"] if k != oid_field))
                  + (" + geometría" if "geometry" in p else "") for p in payload]
        logger.info("%s: %s actualización(es) -> %s%s", self.key, len(detail),
                    "; ".join(detail[:10]), " ..." if len(detail) > 10 else "")
        ok, errors = 0, []
        for chunk in _chunks(payload):
            result = self.get_layer().edit_features(updates=chunk)
            for r in result.get("updateResults", []):
                if r.get("success"):
                    ok += 1
                else:
                    errors.append(f"{self.key} oid={r.get('objectId')}: {r.get('error')}")
        return ok, errors

    def add_features(self, adds: list[dict[str, Any]]) -> tuple[int, list[str]]:
        """adds: [{"attributes": {...}, "geometry": {...}}]."""
        if not adds:
            return 0, []
        payload = [{"attributes": self._existing_only(a["attributes"]),
                    "geometry": a["geometry"]} for a in adds]
        ok, errors = 0, []
        for chunk in _chunks(payload):
            result = self.get_layer().edit_features(adds=chunk)
            for r in result.get("addResults", []):
                if r.get("success"):
                    ok += 1
                else:
                    errors.append(f"{self.key} alta: {r.get('error')}")
        return ok, errors

    def delete_features(self, object_ids: list[int]) -> int:
        if not object_ids:
            return 0
        result = self.get_layer().edit_features(deletes=object_ids)
        return sum(1 for r in result.get("deleteResults", []) if r.get("success"))

    # ---------------------------------------------------------- adjuntos --

    def linked_attachments(self) -> list[dict[str, Any]]:
        """
        Adjuntos (fotos) de todas las features vinculadas, en UNA consulta
        (queryAttachments). Si el servicio no la soporta, cae a una consulta
        por feature.
        """
        if not self.has_attachments():
            return []
        layer = self.get_layer()
        try:
            rows = layer.attachments.search(where=f"{self.id_field} > 0")
            return [{
                "parent_oid": r["PARENTOBJECTID"], "id": r["ID"], "name": r["NAME"],
                "content_type": r.get("CONTENTTYPE"), "size": r.get("SIZE") or 0,
                "global_id": r.get("GLOBALID"),
            } for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.info("queryAttachments no disponible en '%s' (%s); consulta por feature.",
                        self.key, exc)
        rows = []
        for f in self.query(f"{self.id_field} > 0", out_fields=[self.oid_field()],
                            return_geometry=False):
            for a in layer.attachments.get_list(f["_oid"]):
                rows.append({
                    "parent_oid": f["_oid"], "id": a["id"], "name": a["name"],
                    "content_type": a.get("contentType"), "size": a.get("size") or 0,
                    "global_id": a.get("globalId"),
                })
        return rows

    def download_attachment(self, object_id: int, attachment_id: int) -> tuple[str, bytes]:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.get_layer().attachments.download(
                oid=int(object_id), attachment_id=int(attachment_id), save_path=tmp,
            )
            path = paths[0]
            with open(path, "rb") as fh:
                return os.path.basename(path), fh.read()


class ArcGISClient(ArcGISLayer):
    """Compatibilidad: capa de solicitudes (puntos) con la configuración histórica."""

    def __init__(self) -> None:
        super().__init__("solicitudes", "Solicitudes", settings.arcgis_feature_layer_item_id,
                         ODOO_ID_FIELD)