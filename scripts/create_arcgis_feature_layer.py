"""
Crea (una sola vez) la Hosted Feature Layer en ArcGIS Online / Enterprise
que usará integration-api para publicar los contactos de Odoo.

Uso:
    pip install arcgis python-dotenv
    python scripts/create_arcgis_feature_layer.py

Al terminar, copia el "Item ID" impreso en consola dentro de
integration-api/.env -> ARCGIS_FEATURE_LAYER_ITEM_ID
"""

import os
from dotenv import load_dotenv
from arcgis.gis import GIS

load_dotenv()

ARCGIS_URL = os.getenv("ARCGIS_URL", "https://www.arcgis.com")
ARCGIS_USERNAME = os.getenv("ARCGIS_USERNAME")
ARCGIS_PASSWORD = os.getenv("ARCGIS_PASSWORD")
LAYER_TITLE = os.getenv("ARCGIS_LAYER_TITLE", "Solicitudes Ciudadanas - Demo Odoo")

FIELDS = [
    {"name": "odoo_partner_id", "type": "esriFieldTypeInteger", "alias": "ID Odoo (res.partner)"},
    {"name": "name", "type": "esriFieldTypeString", "alias": "Descripción", "length": 255},
    {"name": "address", "type": "esriFieldTypeString", "alias": "Dirección", "length": 255},
    {"name": "city", "type": "esriFieldTypeString", "alias": "Ciudad", "length": 128},
    {"name": "phone", "type": "esriFieldTypeString", "alias": "Teléfono", "length": 64},
    {"name": "email", "type": "esriFieldTypeString", "alias": "Correo", "length": 128},
    {"name": "status", "type": "esriFieldTypeString", "alias": "Estado", "length": 64},
    {"name": "last_sync", "type": "esriFieldTypeDate", "alias": "Última sincronización"},
]


def main() -> None:
    if not ARCGIS_USERNAME or not ARCGIS_PASSWORD:
        raise SystemExit("Define ARCGIS_USERNAME y ARCGIS_PASSWORD (env o .env).")

    gis = GIS(ARCGIS_URL, ARCGIS_USERNAME, ARCGIS_PASSWORD)
    print(f"Conectado como {gis.users.me.username} en {ARCGIS_URL}")

    existing = gis.content.search(f'title:"{LAYER_TITLE}" AND type:"Feature Service"',
                                   max_items=1)
    if existing:
        item = existing[0]
        print(f"La capa ya existe: {item.title} (id={item.id}). No se crea de nuevo.")
        return

    create_params = {
        "name": LAYER_TITLE.replace(" ", "_"),
        "serviceDescription": "Demo de integración Odoo -> ArcGIS (municipios).",
        "hasStaticData": False,
        "maxRecordCount": 2000,
        "capabilities": "Create,Delete,Query,Update,Editing",
        "spatialReference": {"wkid": 4326},
        "geometryType": "esriGeometryPoint",
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
            *FIELDS,
        ],
    }

    item = gis.content.create_service(
        name=create_params["name"],
        create_params=create_params,
        service_type="featureService",
    )
    item.update(item_properties={"title": LAYER_TITLE, "tags": "odoo,municipio,demo"})

    # Hacerla pública para poder mostrarla fácilmente en un Web Map/Dashboard de demo.
    # En un entorno real, ajustar el sharing según la política del municipio.
    item.share(everyone=False, org=True)

    print("\nCapa creada correctamente.")
    print(f"Título:   {item.title}")
    print(f"Item ID:  {item.id}")
    print(f"URL:      {item.url}")
    print("\n-> Copia el Item ID en integration-api/.env como ARCGIS_FEATURE_LAYER_ITEM_ID")


if __name__ == "__main__":
    main()
