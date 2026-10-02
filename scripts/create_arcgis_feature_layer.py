"""
Crea (una sola vez) la capa de entidades alojada de SOLICITUDES que usa
integration-api. Es la alternativa por código a publicar
scripts/plantilla_capa_arcgis.csv desde ArcGIS Online (README, paso 4).

Autenticación: usuario y contraseña (ARCGIS_USERNAME / ARCGIS_PASSWORD).
No funciona con cuentas con MFA o inicio de sesión corporativo (SSO): en ese
caso se publica el CSV desde el navegador. Pensado para ArcGIS Enterprise
con usuarios del portal.

Después de crearla se ejecuta scripts/setup_field_maps.py, que agrega los
campos de sincronización, la lista de estados, los adjuntos y la capa de
frentes de trabajo.

Uso (desde la carpeta scripts, con las variables en scripts/.env):
    pip install -r requirements.txt
    python create_arcgis_feature_layer.py
"""

import os
import time

from dotenv import load_dotenv
from arcgis.features import FeatureLayerCollection
from arcgis.gis import GIS

load_dotenv()

ARCGIS_URL = os.getenv("ARCGIS_URL", "https://www.arcgis.com")
ARCGIS_USERNAME = os.getenv("ARCGIS_USERNAME")
ARCGIS_PASSWORD = os.getenv("ARCGIS_PASSWORD")
ARCGIS_VERIFY_CERT = os.getenv("ARCGIS_VERIFY_CERT", "True").lower() != "false"
LAYER_TITLE = os.getenv("ARCGIS_LAYER_TITLE", "Solicitudes Ciudadanas - Demo Odoo")

WGS84 = {"wkid": 4326}
EXTENT = {"xmin": -180, "ymin": -90, "xmax": 180, "ymax": 90, "spatialReference": WGS84}


def _field(name, ftype, alias, length=None):
    field = {"name": name, "type": ftype, "alias": alias, "nullable": True,
             "editable": True, "domain": None, "defaultValue": None}
    if length:
        field["length"] = length
    return field


FIELDS = [
    {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID",
     "nullable": False, "editable": False},
    {"name": "GlobalID", "type": "esriFieldTypeGlobalID", "alias": "GlobalID", "length": 38,
     "nullable": False, "editable": False},
    _field("odoo_partner_id", "esriFieldTypeInteger", "ID de la solicitud en Odoo"),
    _field("name", "esriFieldTypeString", "Problema reportado", 255),
    _field("citizen_name", "esriFieldTypeString", "Nombre del ciudadano", 255),
    _field("address", "esriFieldTypeString", "Dirección", 255),
    _field("city", "esriFieldTypeString", "Ciudad", 128),
    # Texto (no entero) para conservar el 0 inicial de los teléfonos.
    _field("phone", "esriFieldTypeString", "Teléfono", 64),
    _field("email", "esriFieldTypeString", "Correo", 128),
    _field("status", "esriFieldTypeString", "Estado", 64),
    _field("latitude", "esriFieldTypeDouble", "Latitud"),
    _field("longitude", "esriFieldTypeDouble", "Longitud"),
]


def main() -> None:
    if not ARCGIS_USERNAME or not ARCGIS_PASSWORD:
        raise SystemExit("Definir ARCGIS_USERNAME y ARCGIS_PASSWORD (variables de entorno o .env).")

    gis = GIS(ARCGIS_URL, ARCGIS_USERNAME, ARCGIS_PASSWORD, verify_cert=ARCGIS_VERIFY_CERT)
    print(f"Conectado como {gis.users.me.username} en {ARCGIS_URL}")

    existing = [i for i in gis.content.search(f'title:"{LAYER_TITLE}"', max_items=20)
                if i.title == LAYER_TITLE and i.type == "Feature Service"]
    if existing:
        item = existing[0]
        print(f"La capa ya existe: {item.title} (id={item.id}). No se crea de nuevo.")
        return

    service_name = f"solicitudes_odoo_{int(time.time())}"
    create_params = {
        "name": service_name,
        "serviceDescription": "Solicitudes ciudadanas sincronizadas con Odoo (demo municipios).",
        "hasStaticData": False,
        "maxRecordCount": 2000,
        "supportedQueryFormats": "JSON",
        "capabilities": "Create,Delete,Query,Update,Editing",
        "spatialReference": WGS84,
        "initialExtent": EXTENT,
        "allowGeometryUpdates": True,
        "units": "esriDecimalDegrees",
    }
    item = gis.content.create_service(name=service_name, create_params=create_params,
                                      service_type="featureService")

    # createService crea el servicio VACÍO: la capa (geometría y campos) se
    # agrega aparte con addToDefinition.
    layer = {
        "id": 0, "name": "Solicitudes", "type": "Feature Layer",
        "geometryType": "esriGeometryPoint", "objectIdField": "OBJECTID",
        "globalIdField": "GlobalID", "displayField": "name", "hasAttachments": False,
        "fields": FIELDS, "extent": EXTENT,
        "drawingInfo": {"renderer": {"type": "simple", "symbol": {
            "type": "esriSMS", "style": "esriSMSCircle", "color": [227, 26, 28, 255], "size": 8,
            "outline": {"color": [255, 255, 255, 255], "width": 1}}}},
        "templates": [{"name": "Solicitud", "description": "",
                       "drawingTool": "esriFeatureEditToolPoint",
                       "prototype": {"attributes": {"status": "In Progress"}}}],
    }
    FeatureLayerCollection.fromitem(item).manager.add_to_definition({"layers": [layer]})
    item.update(item_properties={"title": LAYER_TITLE, "tags": "odoo,municipio,demo"})
    try:
        item.sharing.sharing_level = "ORGANIZATION"
    except AttributeError:          # versiones anteriores de la API de Python
        item.share(everyone=False, org=True)

    print("\nCapa creada correctamente.")
    print(f"Título:   {item.title}")
    print(f"Item ID:  {item.id}")
    print(f"URL:      {item.url}")
    print("\n-> Copiar el Item ID en integration-api/.env como ARCGIS_FEATURE_LAYER_ITEM_ID")
    print("-> Siguiente: scripts/setup_field_maps.py (campos de sincronización, lista de "
          "estados, adjuntos, Sync/ChangeTracking y capa de frentes).")


if __name__ == "__main__":
    main()
