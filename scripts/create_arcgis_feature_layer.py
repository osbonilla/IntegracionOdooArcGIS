"""
Crea (una sola vez) la Hosted Feature Layer que usará integration-api.

IMPORTANTE sobre MFA: este script se autentica con usuario/password
(ARCGIS_USERNAME/ARCGIS_PASSWORD). Si tu cuenta de ArcGIS Online tiene
autenticación multifactor (MFA) o login corporativo (SSO), este login
NO va a funcionar (Esri lo rechaza aunque la contraseña sea correcta).
En ese caso, publica la capa manualmente desde el navegador (donde el
MFA sí funciona con normalidad) usando plantilla_capa_arcgis.csv como
punto de partida — ver README sección 5.2.

Este script sigue siendo la vía recomendada para ArcGIS Enterprise con
autenticación básica habilitada (sin MFA), donde si funciona sin problema.

Uso:
    pip install -r requirements.txt
    python create_arcgis_feature_layer.py
"""

import os
from dotenv import load_dotenv
from arcgis.gis import GIS

load_dotenv()

ARCGIS_URL = os.getenv("ARCGIS_URL", "https://www.arcgis.com")
ARCGIS_USERNAME = os.getenv("ARCGIS_USERNAME")
ARCGIS_PASSWORD = os.getenv("ARCGIS_PASSWORD")
ARCGIS_VERIFY_CERT = os.getenv("ARCGIS_VERIFY_CERT", "True").lower() != "false"
LAYER_TITLE = os.getenv("ARCGIS_LAYER_TITLE", "Solicitudes Ciudadanas - Demo Odoo")

FIELDS = [
    {"name": "odoo_partner_id", "type": "esriFieldTypeInteger", "alias": "ID de la solicitud en Odoo"},
    {"name": "name", "type": "esriFieldTypeString", "alias": "Descripción", "length": 255},
    {"name": "address", "type": "esriFieldTypeString", "alias": "Dirección", "length": 255},
    {"name": "city", "type": "esriFieldTypeString", "alias": "Ciudad", "length": 128},
    {"name": "phone", "type": "esriFieldTypeString", "alias": "Teléfono", "length": 64},
    {"name": "email", "type": "esriFieldTypeString", "alias": "Correo", "length": 128},
    {"name": "status", "type": "esriFieldTypeString", "alias": "Estado", "length": 64},
    {"name": "latitude", "type": "esriFieldTypeDouble", "alias": "Latitud"},
    {"name": "longitude", "type": "esriFieldTypeDouble", "alias": "Longitud"},
]


def main() -> None:
    if not ARCGIS_USERNAME or not ARCGIS_PASSWORD:
        raise SystemExit("Define ARCGIS_USERNAME y ARCGIS_PASSWORD (env o .env).")

    gis = GIS(ARCGIS_URL, ARCGIS_USERNAME, ARCGIS_PASSWORD, verify_cert=ARCGIS_VERIFY_CERT)
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
    item.share(everyone=False, org=True)

    print("\nCapa creada correctamente.")
    print(f"Título:   {item.title}")
    print(f"Item ID:  {item.id}")
    print(f"URL:      {item.url}")
    print("\n-> Copia el Item ID en integration-api/.env como ARCGIS_FEATURE_LAYER_ITEM_ID")
    print(
        "-> Si vas a usar Webhooks nativos de la capa (ver README sección 6.1), "
        "actívalo desde el Portal: capa -> Settings -> Editing -> "
        "'Keep track of changes to the data'."
    )


if __name__ == "__main__":
    main()
