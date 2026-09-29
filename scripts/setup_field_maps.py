"""
Prepara las capas de ArcGIS para Field Maps y la sincronización con Odoo.

Es idempotente: se puede ejecutar varias veces; solo agrega lo que falta
(si la capa "Frentes de Trabajo" ya existe, la reutiliza en vez de crear otra).

1. Capa de SOLICITUDES (puntos, la que ya existe):
   - campos nuevos: frente_id, frente_nombre, observaciones,
     sync_status, sync_geom, sync_obs
   - lista de valores (dominio) en `status`: el código guardado es la
     etiqueta de Odoo ("Done"); en Field Maps se ve en español ("Hecho")
   - adjuntos habilitados (fotos)
2. Capa de FRENTES DE TRABAJO (líneas): la crea con --crear-frentes, o
   completa una existente con --frentes <item_id>.
3. En ambos servicios: Sync + ChangeTracking (requisito de los webhooks y
   de Field Maps offline) y editor tracking (quién/cuándo editó).

Uso en un ArcGIS Notebook (recomendado; obligatorio con MFA en ArcGIS Online):
    1. Pega TODO este archivo en una celda y ejecútala (solo carga funciones).
    2. En otra celda, con el item id de la capa de solicitudes (el mismo
       ARCGIS_FEATURE_LAYER_ITEM_ID de integration-api/.env):
           from arcgis.gis import GIS
           gis = GIS("home")
           preparar(gis, solicitudes_item_id="<ITEM_ID_PUNTOS>", crear_frentes=True)
           acelerar_webhooks(gis, "<ITEM_ID_PUNTOS>")   # webhook de la capa cada 30 s

Uso local desde VS Code / PowerShell con ArcGIS Pro (funciona con MFA: usa la
sesión que ya tienes iniciada en ArcGIS Pro). Se ejecuta con el Python de Pro:
    & "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe" `
        scripts\\setup_field_maps.py --pro --crear-frentes --acelerar-webhooks

Uso local con usuario y contraseña (ArcGIS Enterprise, o cuentas sin MFA):
    python setup_field_maps.py --url https://www.arcgis.com --user USUARIO \\
        --solicitudes <ITEM_ID_PUNTOS> --crear-frentes

    Sin --solicitudes toma ARCGIS_FEATURE_LAYER_ITEM_ID (y sin --url/--user,
    ARCGIS_URL / ARCGIS_USERNAME / ARCGIS_PASSWORD) de ../integration-api/.env
    (--env-file).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time

# Estados de project.task en Odoo 17/18 (código de la etiqueta en inglés,
# que es lo que la integración lee por XML-RPC) -> nombre que ve la
# cuadrilla. "Waiting" no se incluye: Odoo lo calcula solo.
ESTADOS = [
    ("In Progress", "En progreso"),
    ("Changes Requested", "Cambios solicitados"),
    ("Approved", "Aprobado"),
    ("Done", "Hecho"),
    ("Cancelled", "Cancelado"),
]
TIPOS_TRABAJO = ["Bacheo", "Alcantarillado", "Agua potable", "Alumbrado público",
                 "Señalización", "Áreas verdes", "Otro"]
TITULO_FRENTES = "Frentes de Trabajo"


class ErrorDeConfiguracion(Exception):
    """Dato de entrada equivocado (item id que no existe, que no es una capa...)."""

    def _render_traceback_(self):
        # Jupyter / ArcGIS Notebook: mostrar solo el mensaje, sin traceback.
        return [f"ERROR: {self}"]


def _dominio_estado():
    return {"type": "codedValue", "name": "estado_odoo",
            "codedValues": [{"name": nombre, "code": code} for code, nombre in ESTADOS]}


def _dominio_tipo():
    return {"type": "codedValue", "name": "tipo_trabajo",
            "codedValues": [{"name": t, "code": t} for t in TIPOS_TRABAJO]}


def _texto(nombre, alias, largo=255, dominio=None):
    return {"name": nombre, "type": "esriFieldTypeString", "alias": alias, "length": largo,
            "nullable": True, "editable": True, "domain": dominio, "defaultValue": None}


def _entero(nombre, alias):
    return {"name": nombre, "type": "esriFieldTypeInteger", "alias": alias,
            "nullable": True, "editable": True, "domain": None, "defaultValue": None}


def _decimal(nombre, alias):
    return {"name": nombre, "type": "esriFieldTypeDouble", "alias": alias,
            "nullable": True, "editable": True, "domain": None, "defaultValue": None}


def _fecha(nombre, alias):
    return {"name": nombre, "type": "esriFieldTypeDate", "alias": alias, "length": 8,
            "nullable": True, "editable": True, "domain": None, "defaultValue": None}


CAMPOS_SOLICITUDES = [
    _entero("frente_id", "Frente de trabajo (id Odoo)"),
    _texto("frente_nombre", "Frente de trabajo"),
    _texto("observaciones", "Observaciones de campo", 1000),
    _texto("sync_status", "sync: estado", 64),
    _texto("sync_geom", "sync: ubicación", 64),
    _texto("sync_obs", "sync: observación", 64),
]

CAMPOS_FRENTES = [
    {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID",
     "nullable": False, "editable": False},
    {"name": "GlobalID", "type": "esriFieldTypeGlobalID", "alias": "GlobalID", "length": 38,
     "nullable": False, "editable": False},
    _entero("odoo_task_id", "Tarea en Odoo"),
    _texto("nombre", "Nombre del frente"),
    _texto("tipo_trabajo", "Tipo de trabajo", 64, _dominio_tipo()),
    _texto("responsable", "Responsable / cuadrilla", 128),
    _texto("status", "Estado", 64, _dominio_estado()),
    _fecha("fecha_inicio", "Fecha de inicio"),
    _fecha("fecha_fin", "Fecha de fin prevista"),
    _texto("observaciones", "Observaciones de campo", 1000),
    _decimal("longitud_m", "Longitud (m)"),
    _entero("solicitudes_asignadas", "Solicitudes asignadas"),
    _texto("sync_status", "sync: estado", 64),
    _texto("sync_fp", "sync: huella", 64),
    _texto("sync_obs", "sync: observación", 64),
]

EDITOR_TRACKING = {
    "enableEditorTracking": True, "enableOwnershipAccessControl": False,
    "allowOthersToQuery": True, "allowOthersToUpdate": True, "allowOthersToDelete": True,
    "allowAnonymousToQuery": True, "allowAnonymousToUpdate": True, "allowAnonymousToDelete": True,
}
CAPACIDADES = {"Query", "Create", "Update", "Delete", "Editing", "Sync", "ChangeTracking"}


def _ok(msg):
    print(f"  [OK] {msg}")


def _falla(msg, exc, manual):
    print(f"  [FALLÓ] {msg}: {exc}\n         Hazlo a mano: {manual}")


# ------------------------------------------------------------ items --------

def _item_de_capa(gis, item_id: str, que: str, geometria: str | None = None, pista: str = ""):
    """El item de una capa de entidades alojada, o un error que explica qué id se esperaba."""
    item = gis.content.get(item_id)
    if item is None:
        raise ErrorDeConfiguracion(
            f"no se encontró el item de {que} '{item_id}' (o tu usuario no tiene acceso). "
            f"El id está en la URL del item: .../home/item.html?id=<ITEM_ID>. {pista}".strip())
    if item.type != "Feature Service" or not item.layers:
        raise ErrorDeConfiguracion(
            f"'{item.title}' ({item.id}) es un item de tipo '{item.type}', no una capa de "
            f"entidades. Usa el item de la capa de {que}. {pista}".strip())
    tipo = item.layers[0].properties.get("geometryType")
    if geometria and tipo and tipo != geometria:
        raise ErrorDeConfiguracion(
            f"'{item.title}' ({item.id}) tiene geometría {tipo}; la capa de {que} "
            f"debe ser {geometria}. ¿Se cruzaron los ids de solicitudes y frentes?")
    return item


def _frentes_existentes(gis) -> list:
    """Items 'Frentes de Trabajo' (capa de entidades) del usuario, del más antiguo al más nuevo."""
    yo = getattr(getattr(gis, "users", None), "me", None)
    if yo is None:
        return []
    try:
        encontrados = gis.content.search(
            query=f'title:"{TITULO_FRENTES}" AND owner:"{yo.username}"', max_items=100)
    except Exception as exc:  # noqa: BLE001
        print(f"  [--] no se pudo buscar una capa '{TITULO_FRENTES}' existente: {exc}")
        return []
    propios = [i for i in encontrados
               if i.type == "Feature Service" and i.title == TITULO_FRENTES]
    return sorted(propios, key=lambda i: i.created or 0)


# ------------------------------------------------------------ capa ---------

def _asegurar_campos(layer, campos):
    existentes = {f["name"].lower() for f in layer.properties.fields}
    faltan = [c for c in campos if c["name"].lower() not in existentes
              and c["type"] not in ("esriFieldTypeOID", "esriFieldTypeGlobalID")]
    if not faltan:
        _ok("todos los campos ya existen")
        return
    try:
        layer.manager.add_to_definition({"fields": faltan})
        _ok("campos agregados: " + ", ".join(c["name"] for c in faltan))
    except Exception as exc:  # noqa: BLE001
        _falla("agregar campos", exc, "Datos > Campos > Agregar, con los nombres del README")


def _asegurar_dominio(layer, campo, dominio):
    actual = next((f for f in layer.properties.fields if f["name"].lower() == campo), None)
    if actual is None:
        print(f"  [--] la capa no tiene el campo '{campo}'")
        return
    codigos = [v["code"] for v in (actual.get("domain") or {}).get("codedValues", [])]
    if codigos == [v["code"] for v in dominio["codedValues"]]:
        _ok(f"lista de valores de '{campo}' ya configurada")
        return
    try:
        layer.manager.update_definition({"fields": [{"name": actual["name"], "domain": dominio}]})
        _ok(f"lista de valores en '{campo}': "
            + ", ".join(f"{v['code']}→{v['name']}" for v in dominio["codedValues"]))
    except Exception as exc:  # noqa: BLE001
        _falla(f"lista de valores en '{campo}'", exc,
               "Datos > Campos > status > Crear lista (códigos = etiquetas de Odoo)")


def _asegurar_adjuntos(layer):
    if layer.properties.get("hasAttachments"):
        _ok("adjuntos ya habilitados")
        return
    try:
        layer.manager.update_definition({"hasAttachments": True})
        _ok("adjuntos habilitados")
    except Exception as exc:  # noqa: BLE001
        _falla("habilitar adjuntos", exc, "Configuración > Capa de entidades > Habilitar adjuntos")


# --------------------------------------------------------- servicio ---------

def _asegurar_servicio(flc):
    props = flc.properties
    actuales = {c.strip() for c in (props.get("capabilities") or "").split(",") if c.strip()}
    if CAPACIDADES - actuales or not props.get("syncEnabled"):
        try:
            flc.manager.update_definition({
                "capabilities": ",".join(sorted(actuales | CAPACIDADES)), "syncEnabled": True,
            })
            _ok("Sync + ChangeTracking habilitados (webhooks y Field Maps offline)")
        except Exception as exc:  # noqa: BLE001
            _falla("habilitar Sync/ChangeTracking", exc,
                   "Configuración > Habilitar sincronización y 'Mantener un registro de los cambios'")
    else:
        _ok("Sync + ChangeTracking ya habilitados")
    if not (props.get("editorTrackingInfo") or {}).get("enableEditorTracking"):
        try:
            flc.manager.update_definition({"editorTrackingInfo": EDITOR_TRACKING})
            _ok("editor tracking habilitado")
        except Exception as exc:  # noqa: BLE001
            _falla("habilitar editor tracking", exc,
                   "Configuración > 'Realizar un seguimiento de quién crea y actualiza entidades'")
    else:
        _ok("editor tracking ya habilitado")


def _crear_frentes(gis):
    from arcgis.features import FeatureLayerCollection

    nombre = f"frentes_trabajo_{int(time.time())}"
    params = {
        "name": nombre, "serviceDescription": "Frentes de trabajo (Field Maps <-> Odoo)",
        "hasStaticData": False, "maxRecordCount": 2000, "supportedQueryFormats": "JSON",
        "capabilities": ",".join(sorted(CAPACIDADES)), "description": "", "copyrightText": "",
        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        "initialExtent": {"xmin": -20037507.07, "ymin": -30240971.96, "xmax": 20037507.07,
                          "ymax": 18398924.32, "spatialReference": {"wkid": 102100, "latestWkid": 3857}},
        "allowGeometryUpdates": True, "units": "esriMeters", "syncEnabled": True,
        "editorTrackingInfo": EDITOR_TRACKING,
        "xssPreventionInfo": {"xssPreventionEnabled": True, "xssPreventionRule": "InputOnly",
                              "xssInputRule": "rejectInvalid"},
    }
    # Las etiquetas van en item_properties (y no en el argumento `tags`) para
    # que funcione también con versiones anteriores de arcgis (ArcGIS Pro 3.x).
    item = gis.content.create_service(
        name=nombre, create_params=params, service_type="featureService",
        item_properties={"title": TITULO_FRENTES, "tags": "odoo,field maps,frentes de trabajo",
                         "snippet": "Frentes de trabajo dibujados en Field Maps y "
                                    "sincronizados con Odoo."},
    )
    capa = {
        "id": 0, "name": TITULO_FRENTES, "type": "Feature Layer",
        "geometryType": "esriGeometryPolyline", "objectIdField": "OBJECTID",
        "globalIdField": "GlobalID", "displayField": "nombre", "hasAttachments": True,
        "fields": CAMPOS_FRENTES,
        "drawingInfo": {"renderer": {"type": "simple", "symbol": {
            "type": "esriSLS", "style": "esriSLSSolid", "color": [230, 115, 0, 255], "width": 4}}},
        "templates": [{"name": "Frente de trabajo", "description": "",
                       "drawingTool": "esriFeatureEditToolLine",
                       "prototype": {"attributes": {"status": "In Progress"}}}],
        "extent": params["initialExtent"],
    }
    FeatureLayerCollection.fromitem(item).manager.add_to_definition({"layers": [capa]})
    _ok(f"capa de líneas creada: '{item.title}' (item {item.id})")
    return item


# ----------------------------------------------------------- principal ------

PISTA_SOLICITUDES = ("Es el mismo valor de ARCGIS_FEATURE_LAYER_ITEM_ID en integration-api/.env "
                     "(la capa de entidades, no el formulario de Survey123).")


def preparar(gis, solicitudes_item_id: str | None = None, crear_frentes: bool = False,
             frentes_item_id: str | None = None) -> dict:
    from arcgis.features import FeatureLayerCollection

    resultado = {}
    if solicitudes_item_id:
        item = _item_de_capa(gis, solicitudes_item_id, "solicitudes", "esriGeometryPoint",
                             PISTA_SOLICITUDES)
        print(f"\n== Solicitudes (puntos): {item.title}")
        capa = item.layers[0]
        _asegurar_campos(capa, CAMPOS_SOLICITUDES)
        _asegurar_dominio(capa, "status", _dominio_estado())
        _asegurar_adjuntos(capa)
        _asegurar_servicio(FeatureLayerCollection.fromitem(item))
        resultado["ARCGIS_FEATURE_LAYER_ITEM_ID"] = item.id

    if crear_frentes and not frentes_item_id:
        existentes = _frentes_existentes(gis)
        if existentes:
            frentes_item_id = existentes[0].id
            print(f"\n== Frentes de trabajo (líneas): ya existe '{TITULO_FRENTES}' "
                  f"(item {frentes_item_id}); se reutiliza")
            if len(existentes) > 1:
                print("  [--] hay otras con el mismo nombre (se ignoran): "
                      + ", ".join(i.id for i in existentes[1:]))
        else:
            print("\n== Frentes de trabajo (líneas): creando capa")
            frentes_item_id = _crear_frentes(gis).id
    if frentes_item_id:
        item = _item_de_capa(gis, frentes_item_id, "frentes de trabajo", "esriGeometryPolyline")
        print(f"\n== Frentes de trabajo (líneas): {item.title}")
        capa = item.layers[0]
        _asegurar_campos(capa, CAMPOS_FRENTES)
        _asegurar_dominio(capa, "status", _dominio_estado())
        _asegurar_dominio(capa, "tipo_trabajo", _dominio_tipo())
        _asegurar_adjuntos(capa)
        _asegurar_servicio(FeatureLayerCollection.fromitem(item))
        resultado["ARCGIS_WORKFRONT_LAYER_ITEM_ID"] = item.id

    print("\nListo. Variables para integration-api/.env:")
    for k, v in resultado.items():
        print(f"  {k}={v}")
    print("\nSiguiente: dar acceso a estos items a las credenciales de App Authentication "
          "(si las usas), armar el mapa web para Field Maps y crear los webhooks "
          "(ver README).")
    return resultado


def acelerar_webhooks(gis, item_id: str, segundos: int = 30, secreto: str | None = None) -> None:
    """
    Los webhooks de una capa se envían por lotes según su "scheduleInfo"; el
    que se crea desde la interfaz puede tardar un par de minutos. Esto lo
    baja al mínimo que permite ArcGIS Online: 30 segundos. Uso en un Notebook:
        acelerar_webhooks(gis, "<ITEM_ID_CAPA>")
    Si al crear el webhook le pusiste un secreto (Secret), pásalo otra vez para
    que se conserve: acelerar_webhooks(gis, "<ITEM_ID_CAPA>", secreto="...")
    """
    from arcgis.features import FeatureLayerCollection

    item = _item_de_capa(gis, item_id, "la capa con el webhook")
    manager = FeatureLayerCollection.fromitem(item).manager.webhook_manager
    hooks = manager.list if manager is not None else []
    if not hooks:
        print(f"  [--] '{item.title}' no tiene webhooks (créalo primero: README 9.2)")
        return
    intervalo = max(30, int(segundos))
    for hook in hooks:
        nombre = hook.properties.get("name")
        antes = dict(hook.properties.get("scheduleInfo") or {}).get("recurrenceInfo")
        cambios = {"schedule_info": {
            "name": f"cada-{intervalo}s",
            "startAt": int(time.time() * 1000),
            "state": "enabled",
            "recurrenceInfo": {"frequency": "second", "interval": intervalo},
        }}
        if secreto:
            cambios["signature_key"] = secreto
        hook.edit(**cambios)
        despues = dict(hook.properties.get("scheduleInfo") or {}).get("recurrenceInfo")
        print(f"  [OK] webhook '{nombre}': {antes} -> {despues}")


def _leer_env(path):
    valores = {}
    if path and os.path.exists(path):
        # utf-8-sig: tolera el BOM que agrega el Bloc de notas de Windows.
        with open(path, encoding="utf-8-sig") as archivo:
            for linea in archivo:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    k, v = linea.split("=", 1)
                    valores[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return valores


def _conectar_con_pro():
    """
    Reutiliza la sesión iniciada en ArcGIS Pro (que ya pasó el MFA en el
    navegador). Requiere ejecutar el script con el Python de Pro (arcpy).
    """
    from arcgis.gis import GIS

    try:
        gis = GIS("pro")
    except Exception as exc:  # noqa: BLE001  (sin arcpy -> ImportError; otros -> ValueError)
        sys.exit(f"No se pudo usar la sesión de ArcGIS Pro: {exc}\n"
                 "Ejecuta el script con el Python de ArcGIS Pro (arcgispro-py3), no con "
                 "el Python normal de VS Code.")
    if getattr(gis.users, "me", None) is None:
        sys.exit("ArcGIS Pro no tiene una sesión iniciada: abre ArcGIS Pro, inicia sesión "
                 "con tu cuenta de ArcGIS Online y vuelve a ejecutar el script.")
    return gis


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=os.path.join(os.path.dirname(__file__), "..",
                                                      "integration-api", ".env"))
    p.add_argument("--pro", action="store_true",
                   help="usa la sesión iniciada en ArcGIS Pro (funciona con MFA); ejecutar "
                        "con el Python de Pro (arcgispro-py3)")
    p.add_argument("--url")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--solicitudes", help="item id de la capa de puntos (por defecto, "
                                         "ARCGIS_FEATURE_LAYER_ITEM_ID del .env)")
    p.add_argument("--frentes", help="item id de una capa de líneas ya existente")
    p.add_argument("--crear-frentes", action="store_true",
                   help="crea la capa de líneas (o reutiliza la tuya si ya existe)")
    p.add_argument("--acelerar-webhooks", action="store_true",
                   help="baja a 30 s el envío de los webhooks de las capas (README 9.4)")
    p.add_argument("--no-verificar-certificado", action="store_true",
                   help="solo para Enterprise de laboratorio con certificado autofirmado")
    a = p.parse_args()

    env = _leer_env(a.env_file)
    if a.pro:
        gis = _conectar_con_pro()
    else:
        url = a.url or env.get("ARCGIS_URL") or "https://www.arcgis.com"
        user = a.user or env.get("ARCGIS_USERNAME")
        password = a.password or env.get("ARCGIS_PASSWORD")
        if not user:
            sys.exit("Falta --user (o ARCGIS_USERNAME en el .env). Con MFA usa --pro "
                     "(ArcGIS Pro) o un ArcGIS Notebook.")
        if not password:
            password = getpass.getpass(f"Contraseña de {user}: ")

        from arcgis.gis import GIS
        gis = GIS(url, user, password, verify_cert=not a.no_verificar_certificado)
    print(f"Conectado a {gis.url} como {gis.users.me.username}")
    try:
        resultado = preparar(gis, a.solicitudes or env.get("ARCGIS_FEATURE_LAYER_ITEM_ID"),
                             crear_frentes=a.crear_frentes, frentes_item_id=a.frentes)
        if a.acelerar_webhooks:
            print("\n== Webhooks de las capas cada 30 s")
            for item_id in resultado.values():
                acelerar_webhooks(gis, item_id)
    except ErrorDeConfiguracion as exc:
        sys.exit(f"ERROR: {exc}")


AYUDA_NOTEBOOK = """
Funciones cargadas (todavía no se cambió nada en ArcGIS).
En una celda nueva ejecuta esto, con el item id de tu capa de solicitudes
(el mismo ARCGIS_FEATURE_LAYER_ITEM_ID de integration-api/.env):

    from arcgis.gis import GIS
    gis = GIS("home")
    preparar(gis, solicitudes_item_id="<ITEM_ID_SOLICITUDES>", crear_frentes=True)
    acelerar_webhooks(gis, "<ITEM_ID_SOLICITUDES>")
"""


def _pegado_en_notebook() -> bool:
    """
    True si este código se pegó en una celda de Jupyter / ArcGIS Notebook: ahí
    __name__ también vale "__main__", pero no hay __file__ ni argumentos de
    consola, así que main() fallaría.
    """
    return "ipykernel" in sys.modules and "__file__" not in globals()


if __name__ == "__main__":
    if _pegado_en_notebook():
        print(AYUDA_NOTEBOOK)
    else:
        main()