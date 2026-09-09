"""
Crea contactos demo en Odoo (res.partner) con coordenadas, simulando
"solicitudes ciudadanas" reportadas en distintos puntos de una ciudad.

Uso:
    pip install python-dotenv
    python scripts/seed_odoo_demo_data.py

Requiere las mismas variables ODOO_* que integration-api/.env
(se leen del entorno o de un .env en el mismo directorio del script).
"""

import os
import xmlrpc.client
from dotenv import load_dotenv

load_dotenv()

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo_demo")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")
TAG_NAME = os.getenv("ODOO_SYNC_TAG", "Solicitud Ciudadana")

# Puntos de ejemplo dentro de una ciudad (ajustar a tu municipio real).
# Por defecto: referencias dentro de Quito, Ecuador.
DEMO_REQUESTS = [
    {"name": "Bache en vía - La Mariscal", "street": "Av. Amazonas y Roca", "city": "Quito",
     "lat": -0.2035, "lon": -78.4919, "phone": "0999000001"},
    {"name": "Alumbrado público dañado - La Floresta", "street": "Isabel La Católica", "city": "Quito",
     "lat": -0.2079, "lon": -78.4941, "phone": "0999000002"},
    {"name": "Fuga de agua - Centro Histórico", "street": "García Moreno", "city": "Quito",
     "lat": -0.2201, "lon": -78.5123, "phone": "0999000003"},
    {"name": "Poda de árbol requerida - La Carolina", "street": "Av. Eloy Alfaro", "city": "Quito",
     "lat": -0.1807, "lon": -78.4859, "phone": "0999000004"},
    {"name": "Señalización vial borrada - El Batán", "street": "Av. 6 de Diciembre", "city": "Quito",
     "lat": -0.1755, "lon": -78.4823, "phone": "0999000005"},
    {"name": "Acumulación de basura - Cotocollao", "street": "Av. Machala", "city": "Quito",
     "lat": -0.1289, "lon": -78.4989, "phone": "0999000006"},
    {"name": "Semáforo intermitente - Villaflora", "street": "Av. Napo", "city": "Quito",
     "lat": -0.2517, "lon": -78.5238, "phone": "0999000007"},
    {"name": "Parque en mal estado - Chillogallo", "street": "Av. Mariscal Sucre", "city": "Quito",
     "lat": -0.2911, "lon": -78.5478, "phone": "0999000008"},
]


def main() -> None:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    if not uid:
        raise SystemExit("No se pudo autenticar en Odoo. Revisa las credenciales/DB.")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    def execute(model, method, *args):
        return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, list(args))

    tag_ids = execute("res.partner.category", "search", [["name", "=", TAG_NAME]])
    if tag_ids:
        tag_id = tag_ids[0]
    else:
        tag_id = execute("res.partner.category", "create", {"name": TAG_NAME})
    print(f"Tag '{TAG_NAME}' -> id {tag_id}")

    created = 0
    for req in DEMO_REQUESTS:
        existing = execute("res.partner", "search", [["name", "=", req["name"]]])
        if existing:
            print(f"Ya existe: {req['name']}")
            continue

        partner_id = execute("res.partner", "create", {
            "name": req["name"],
            "street": req["street"],
            "city": req["city"],
            "phone": req["phone"],
            "partner_latitude": req["lat"],
            "partner_longitude": req["lon"],
            "category_id": [(4, tag_id)],
        })
        print(f"Creado: {req['name']} -> id {partner_id}")
        created += 1

    print(f"\nListo. {created} contactos nuevos creados con tag '{TAG_NAME}'.")


if __name__ == "__main__":
    main()
