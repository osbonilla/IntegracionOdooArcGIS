"""
Crea datos demo en Odoo: un proyecto "Solicitudes Ciudadanas" con una
tarea (project.task) por cada solicitud, vinculada a un contacto
(res.partner) que representa al ciudadano y lleva la geolocalización.

Requiere el módulo Project activado en Odoo (Apps -> Project -> Activate).
"""

import os
import xmlrpc.client
from dotenv import load_dotenv

load_dotenv()

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo_demo")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "admin")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD", "admin")
PROJECT_NAME = os.getenv("ODOO_PROJECT_NAME", "Solicitudes Ciudadanas")

DEMO_REQUESTS = [
    {"citizen": "Juan Pérez", "issue": "Bache en vía - La Mariscal",
     "street": "Av. Amazonas y Roca", "city": "Quito",
     "lat": -0.2035, "lon": -78.4919, "phone": "0999000001"},
    {"citizen": "María Torres", "issue": "Alumbrado público dañado - La Floresta",
     "street": "Isabel La Católica", "city": "Quito",
     "lat": -0.2079, "lon": -78.4941, "phone": "0999000002"},
    {"citizen": "Carlos Ruiz", "issue": "Fuga de agua - Centro Histórico",
     "street": "García Moreno", "city": "Quito",
     "lat": -0.2201, "lon": -78.5123, "phone": "0999000003"},
    {"citizen": "Ana Vega", "issue": "Poda de árbol requerida - La Carolina",
     "street": "Av. Eloy Alfaro", "city": "Quito",
     "lat": -0.1807, "lon": -78.4859, "phone": "0999000004"},
    {"citizen": "Luis Herrera", "issue": "Señalización vial borrada - El Batán",
     "street": "Av. 6 de Diciembre", "city": "Quito",
     "lat": -0.1755, "lon": -78.4823, "phone": "0999000005"},
    {"citizen": "Paola Castro", "issue": "Acumulación de basura - Cotocollao",
     "street": "Av. Machala", "city": "Quito",
     "lat": -0.1289, "lon": -78.4989, "phone": "0999000006"},
    {"citizen": "Diego Salazar", "issue": "Semáforo intermitente - Villaflora",
     "street": "Av. Napo", "city": "Quito",
     "lat": -0.2517, "lon": -78.5238, "phone": "0999000007"},
    {"citizen": "Sofía Núñez", "issue": "Parque en mal estado - Chillogallo",
     "street": "Av. Mariscal Sucre", "city": "Quito",
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

    project_ids = execute("project.project", "search", [["name", "=", PROJECT_NAME]])
    if project_ids:
        project_id = project_ids[0]
    else:
        project_id = execute("project.project", "create", {"name": PROJECT_NAME})
    print(f"Proyecto '{PROJECT_NAME}' -> id {project_id}")

    created = 0
    for req in DEMO_REQUESTS:
        existing_task = execute("project.task", "search", [["name", "=", req["issue"]]])
        if existing_task:
            print(f"Ya existe: {req['issue']}")
            continue

        partner_id = execute("res.partner", "create", {
            "name": req["citizen"],
            "street": req["street"],
            "city": req["city"],
            "phone": req["phone"],
            "partner_latitude": req["lat"],
            "partner_longitude": req["lon"],
        })

        task_id = execute("project.task", "create", {
            "name": req["issue"],
            "project_id": project_id,
            "partner_id": partner_id,
        })
        print(f"Creado: {req['issue']} (ciudadano: {req['citizen']}) -> tarea id {task_id}")
        created += 1

    print(f"\nListo. {created} solicitudes nuevas creadas en el proyecto '{PROJECT_NAME}'.")


if __name__ == "__main__":
    main()