"""
Pipeline de sincronización bidireccional Odoo <-> ArcGIS.

Una "pasada" ejecuta, en orden y de forma idempotente:

  1. reverse     Features nuevas de campo (Survey123 / Field Maps) sin vínculo
                 -> tarea nueva en Odoo (claim/release: nunca duplica).
  2. merge       Features vinculadas <-> tareas: merge de TRES VÍAS por campo
                 (valor en ArcGIS, valor en Odoo, último valor sincronizado).
  3. reconcile   Features cuya tarea ya no existe en Odoo -> se borran.
  4. assign      Cada solicitud se asigna al frente de trabajo (no cancelado)
                 más cercano dentro de WORKFRONT_BUFFER_M metros.
  5. attachments Fotos/adjuntos de campo -> adjuntos + nota en la tarea.

Principios:
- Toda escritura es por DIFERENCIA: si el valor ya es el correcto, no se
  escribe. Por eso los "ecos" (nuestra propia escritura dispara un webhook
  que dispara otra pasada) convergen: la pasada siguiente no encuentra nada
  que hacer y no escribe nada.
- El microservicio no guarda estado: la "base" del merge vive en campos
  ocultos de cada feature (sync_status, sync_geom, sync_fp, sync_obs).
- Los webhooks (ArcGIS y Odoo) son un "timbre": solo avisan que algo cambió
  y disparan una pasada. Nunca se confía en el contenido del payload, así
  que un webhook duplicado, perdido o falso no puede corromper datos.

Propiedad de cada dato (quién manda):
- Solicitudes: nombre/descr., ciudadano, dirección, teléfono, email -> Odoo.
  Estado y ubicación -> compartidos (merge de tres vías).
  Observaciones y fotos -> campo (van a Odoo como notas/adjuntos).
- Frentes: nombre, tipo, responsable, fechas y trazado -> campo.
  Estado -> compartido. Asignación de solicitudes -> calculada.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from app import geo
from app.arcgis_client import ArcGISLayer, CLAIM_SENTINEL, ODOO_ID_FIELD, SKIP, coerce_value
from app.config import settings
from app.odoo_client import FRONT_TAG_PREFIX, OdooClient
from app.schemas import LayerCounts, PipelineSummary

logger = logging.getLogger("integration.sync")

odoo = OdooClient()
requests_layer = ArcGISLayer("solicitudes", "Solicitud",
                             settings.arcgis_feature_layer_item_id, ODOO_ID_FIELD)
fronts_layer = ArcGISLayer("frentes", "Frente de trabajo",
                           settings.arcgis_workfront_layer_item_id, "odoo_task_id")

# ------------------------------------------------------------ campos -----
F_STATUS = "status"
F_OBS = "observaciones"
F_SYNC_STATUS = "sync_status"   # último estado sincronizado (base del merge)
F_SYNC_GEOM = "sync_geom"       # última ubicación sincronizada "lat,lon"
F_SYNC_FP = "sync_fp"           # huella del contenido de campo de un frente
F_SYNC_OBS = "sync_obs"         # huella de la última observación enviada
F_LAT, F_LON = "latitude", "longitude"
F_FRENTE_ID, F_FRENTE_NOMBRE = "frente_id", "frente_nombre"
F_NOMBRE, F_TIPO, F_RESPONSABLE = "nombre", "tipo_trabajo", "responsable"
F_INICIO, F_FIN = "fecha_inicio", "fecha_fin"
F_LONGITUD, F_ASIGNADAS = "longitud_m", "solicitudes_asignadas"

ALL_STEPS = ("reverse", "merge", "reconcile", "assign", "attachments")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_TAG_RE = re.compile(rf"^{re.escape(FRONT_TAG_PREFIX)}(\d+)\b")

_run_lock = threading.Lock()
_last_summary: PipelineSummary | None = None
_warned: set[str] = set()


# =============================================================== helpers ===

def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _norm(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return value


def _differs(a: Any, b: Any, relative: bool = False) -> bool:
    a, b = _norm(a), _norm(b)
    if isinstance(a, float) or isinstance(b, float):
        try:
            fa, fb = float(a or 0), float(b or 0)
        except (TypeError, ValueError):
            return a != b
        # Campos Single (float32) guardan ~7 cifras: tolerancia relativa.
        tolerance = max(1e-6, abs(fb) * 1e-6) if relative else 1e-6
        return abs(fa - fb) > tolerance
    return a != b


def _set(upd: dict, layer: ArcGISLayer, feature: dict, field: str, value: Any) -> None:
    """
    Agrega field=value al update SOLO si la capa tiene el campo y el valor,
    convertido a como lo guarda ArcGIS en ese campo, es distinto del actual.
    """
    info = layer.field_info(field)
    coerced = coerce_value(info, value)
    if coerced is SKIP:
        return
    single = (info or {}).get("type") == "esriFieldTypeSingle"
    if _differs(feature.get(field), coerced, relative=single):
        upd[field] = coerced


def _arcgis_ts(layer: ArcGISLayer, feature: dict) -> datetime | None:
    field = layer.edit_fields()["edit_date"]
    value = feature.get(field) if field else None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _odoo_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _editor(layer: ArcGISLayer, feature: dict) -> str | None:
    field = layer.edit_fields()["editor"]
    return feature.get(field) if field else None


def _creator(layer: ArcGISLayer, feature: dict) -> str | None:
    field = layer.edit_fields().get("creator")
    return (feature.get(field) if field else None) or _editor(layer, feature)


def _by(editor: str | None) -> str:
    return f" (por {_esc(editor)})" if editor else ""


def _date_value(value: Any) -> datetime | None:
    """Fecha de ArcGIS: epoch ms (esriFieldTypeDate) o 'YYYY-MM-DD' (DateOnly)."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if isinstance(value, str) and len(value) >= 10:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _map_links(lat: float | None, lon: float | None) -> str:
    """Enlaces al mapa web y a Field Maps (solo si ARCGIS_WEBMAP_ID está configurado)."""
    webmap = settings.arcgis_webmap_id
    if not webmap or lat is None or lon is None:
        return ""
    portal = settings.arcgis_url.rstrip("/")
    viewer = (f"{portal}/apps/mapviewer/index.html?webmap={webmap}"
              f"&center={lon:.6f},{lat:.6f}&level=18")
    field_maps = (f"https://fieldmaps.arcgis.app/?referenceContext=center&itemID={webmap}"
                  f"&center={lat:.6f},{lon:.6f}&scale=2000")
    if "arcgis.com" not in portal:
        field_maps += f"&portalURL={portal}"
    return (f'<p><a href="{_esc(viewer)}" target="_blank">Ver en el mapa</a> · '
            f'<a href="{_esc(field_maps)}" target="_blank">Abrir en Field Maps</a></p>')


def _three_way(arc: Any, odo: Any, base: Any, arc_ts: datetime | None,
               odoo_ts: datetime | None, eq: Callable[[Any, Any], bool] = lambda a, b: a == b) -> str:
    """
    Merge de tres vías de UN valor.

    Devuelve:
      "none"            ya coinciden y la base está al día
      "base"            coinciden, solo hay que actualizar la base
      "to_odoo"         solo cambió ArcGIS -> escribir en Odoo
      "to_arcgis"       solo cambió Odoo (o no hay base) -> escribir en ArcGIS
      "conflict_arcgis" cambiaron ambos; ArcGIS es más reciente
      "conflict_odoo"   cambiaron ambos; Odoo es más reciente (o no se sabe)
    """
    if eq(arc, odo):
        return "none" if base is not None and eq(base, arc) else "base"
    if base is None:
        # Sin base (feature anterior a esta versión o capa sin campos sync_*):
        # comportamiento histórico, manda Odoo.
        return "to_arcgis"
    arc_changed, odoo_changed = not eq(arc, base), not eq(odo, base)
    if arc_changed and not odoo_changed:
        return "to_odoo"
    if odoo_changed and not arc_changed:
        return "to_arcgis"
    if arc_ts and odoo_ts and arc_ts > odoo_ts:
        return "conflict_arcgis"
    return "conflict_odoo"


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


def _check_schema(layer: ArcGISLayer, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if not layer.has_field(f)]
    if missing:
        _warn_once(f"schema:{layer.key}",
                   f"La capa '{layer.key}' no tiene los campos {missing}: las ediciones "
                   f"de campo sobre esos datos no se sincronizan. Ejecuta "
                   f"scripts/setup_field_maps.py (ver README).")
    if not layer.edit_fields()["edit_date"]:
        _warn_once(f"tracking:{layer.key}",
                   f"La capa '{layer.key}' no tiene editor tracking: en un conflicto "
                   f"(mismo dato editado en ambos lados) gana Odoo.")


# ============================================================ estado / obs ===

def _merge_status(layer: ArcGISLayer, f: dict, task_id: int, odoo_label: str,
                  odoo_write_date: str | None, upd: dict, c: LayerCounts) -> None:
    if not layer.has_field(F_STATUS):
        return
    arc, odo = _norm(f.get(F_STATUS)), _norm(odoo_label)
    base = (_norm(f.get(F_SYNC_STATUS)) or None) if layer.has_field(F_SYNC_STATUS) else None
    decision = _three_way(arc, odo, base, _arcgis_ts(layer, f), _odoo_ts(odoo_write_date))

    if decision == "none":
        return
    if decision == "base":
        _set(upd, layer, f, F_SYNC_STATUS, odo)
        return
    if decision in ("to_odoo", "conflict_arcgis"):
        code = odoo.state_code(arc)
        if code is None:
            # Valor escrito en campo que Odoo no acepta (vacío, "Waiting",
            # texto libre si la capa no tiene dominio): se revierte.
            c.rejected_field_values += 1
            if arc:
                odoo.post_note(task_id, f"<p>El estado <b>{_esc(arc)}</b> escrito en campo"
                                        f"{_by(_editor(layer, f))} no es válido en Odoo; "
                                        f"se mantiene <b>{_esc(odo)}</b>.</p>")
            _set(upd, layer, f, F_STATUS, odo)
            _set(upd, layer, f, F_SYNC_STATUS, odo)
            return
        odoo.set_task_state_code(task_id, code)
        c.updated_in_odoo += 1
        if decision == "conflict_arcgis":
            c.conflicts += 1
        odoo.post_note(task_id, f"<p>Estado actualizado en campo: <b>{_esc(arc)}</b>"
                                f"{_by(_editor(layer, f))}</p>")
        _set(upd, layer, f, F_SYNC_STATUS, arc)
        return
    # to_arcgis / conflict_odoo
    if decision == "conflict_odoo":
        c.conflicts += 1
    _set(upd, layer, f, F_STATUS, odo)
    _set(upd, layer, f, F_SYNC_STATUS, odo)


def _merge_observations(layer: ArcGISLayer, f: dict, task_id: int, upd: dict,
                        c: LayerCounts) -> None:
    """Observación nueva o cambiada en campo -> nota en el chatter (una sola vez)."""
    if not (layer.has_field(F_OBS) and layer.has_field(F_SYNC_OBS)):
        return
    text = _norm(f.get(F_OBS))
    fp = geo.text_fp(text)
    if fp == _norm(f.get(F_SYNC_OBS)):
        return
    if text:
        odoo.post_note(task_id, f"<p><b>Observación de campo</b>{_by(_editor(layer, f))}:</p>"
                                f"<p>{_esc(text)}</p>")
        c.notes_posted += 1
    _set(upd, layer, f, F_SYNC_OBS, fp)


# ================================================================ reverse ===

def _claim(layer: ArcGISLayer, oid: int, errors: list[str]) -> bool:
    try:
        if layer.claim_feature(oid):
            return True
        logger.warning("%s oid=%s: no se pudo reclamar (otra corrida la tomó).", layer.key, oid)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{layer.key} oid={oid}: error reclamando feature: {exc}")
    return False


def _link(layer: ArcGISLayer, oid: int, task_id: int, attrs: dict) -> None:
    """
    Escribe el vínculo + atributos iniciales. Si falla, reintenta solo el
    vínculo: una tarea ya creada en Odoo NUNCA debe quedar sin vincular,
    porque la feature volvería a NULL y la próxima pasada crearía otra.
    """
    ok, errs = layer.update_features([{"oid": oid, "attributes": attrs}])
    if ok:
        return
    ok, errs2 = layer.update_features([{"oid": oid, "attributes": {layer.id_field: task_id}}])
    if not ok:
        raise RuntimeError(
            f"tarea {task_id} creada en Odoo pero no se pudo vincular la feature {oid} "
            f"(queda reclamada con {CLAIM_SENTINEL}; revisar a mano): {errs + errs2}"
        )


def _initial_status(task_id: int, record: dict) -> str:
    """Si en campo ya eligieron un estado válido, se respeta; si no, el de Odoo."""
    label = _norm(record.get(F_STATUS))
    code = odoo.state_code(label) if label else None
    if code:
        odoo.set_task_state_code(task_id, code)
        return label
    return odoo.get_task_state_label(task_id)


def _reverse_requests(c: LayerCounts, errors: list[str]) -> None:
    layer = requests_layer
    for rec in layer.unlinked_features():
        oid = rec["_oid"]
        if not _claim(layer, oid, errors):
            continue
        task_id = None
        try:
            task_id = odoo.create_task_from_arcgis(rec, settings.odoo_project_name)
            status = _initial_status(task_id, rec)
            attrs: dict[str, Any] = {layer.id_field: task_id, F_STATUS: status,
                                     F_SYNC_STATUS: status}
            if geo.valid_latlon(rec["_lat"], rec["_lon"]):
                attrs.update({F_SYNC_GEOM: geo.point_fp(rec["_lat"], rec["_lon"]),
                              F_LAT: rec["_lat"], F_LON: rec["_lon"]})
            editor = _creator(layer, rec)
            odoo.post_note(task_id, f"<p>Solicitud creada desde campo (ArcGIS){_by(editor)}.</p>"
                                    + _map_links(rec["_lat"], rec["_lon"]))
            text = _norm(rec.get(F_OBS))
            if text and layer.has_field(F_SYNC_OBS):
                odoo.post_note(task_id, f"<p><b>Observación de campo</b>{_by(editor)}:</p>"
                                        f"<p>{_esc(text)}</p>")
                c.notes_posted += 1
                attrs[F_SYNC_OBS] = geo.text_fp(text)
            _link(layer, oid, task_id, attrs)
            c.created_in_odoo += 1
            logger.info("Solicitud creada en Odoo desde ArcGIS: task_id=%s (oid=%s)", task_id, oid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{layer.key} oid={oid}: {exc}")
            if task_id is None:
                _release(layer, oid)


def _release(layer: ArcGISLayer, oid: int) -> None:
    try:
        layer.release_claim(oid)
    except Exception as exc:  # noqa: BLE001
        logger.error("Falló revertir el claim de %s oid=%s: %s", layer.key, oid, exc)


def _front_content(f: dict) -> dict[str, Any]:
    """Contenido 'de campo' de un frente: si su huella cambia, se actualiza Odoo."""
    return {
        "nombre": _norm(f.get(F_NOMBRE)),
        "tipo": _norm(f.get(F_TIPO)),
        "responsable": _norm(f.get(F_RESPONSABLE)),
        "inicio": f.get(F_INICIO),
        "fin": f.get(F_FIN),
        "paths": geo.rounded_paths(f.get("_geom")),
    }


def _front_task_vals(f: dict) -> tuple[dict[str, Any], float]:
    content = _front_content(f)
    length = geo.polyline_length_m(f.get("_geom"))
    mid = geo.polyline_midpoint(f.get("_geom"))
    start, end = _date_value(content["inicio"]), _date_value(content["fin"])
    rows = [
        ("Tipo de trabajo", content["tipo"]),
        ("Responsable", content["responsable"]),
        ("Longitud", f"{length:,.0f} m".replace(",", ".")),
        ("Inicio", start.strftime("%d/%m/%Y") if start else ""),
        ("Fin previsto", end.strftime("%d/%m/%Y") if end else ""),
    ]
    items = "".join(f"<li><b>{k}:</b> {_esc(v)}</li>" for k, v in rows if v)
    description = (f"<ul>{items}</ul>"
                   + (_map_links(mid[0], mid[1]) if mid else "")
                   + "<p><i>Datos del frente sincronizados desde ArcGIS Field Maps: "
                     "se editan en campo.</i></p>")
    vals: dict[str, Any] = {
        "name": content["nombre"] or f"Frente de trabajo (ArcGIS #{f['_oid']})",
        "description": description,
        # date_deadline es Datetime en Odoo 17/18; mediodía UTC para que la
        # fecha no cambie de día al mostrarse en la zona horaria local.
        "date_deadline": end.strftime("%Y-%m-%d 12:00:00") if end else False,
    }
    return vals, length


def _reverse_fronts(c: LayerCounts, errors: list[str]) -> None:
    layer = fronts_layer
    for rec in layer.unlinked_features():
        oid = rec["_oid"]
        if not rec.get("_geom"):
            continue  # sin trazado todavía (p. ej. sincronización offline a medias)
        if not _claim(layer, oid, errors):
            continue
        task_id = None
        try:
            vals, length = _front_task_vals(rec)
            task_id = odoo.create_workfront_task(settings.odoo_workfront_project_name, vals)
            status = _initial_status(task_id, rec)
            attrs: dict[str, Any] = {
                layer.id_field: task_id, F_STATUS: status, F_SYNC_STATUS: status,
                F_SYNC_FP: geo.content_fp(_front_content(rec)), F_LONGITUD: round(length, 1),
            }
            editor = _creator(layer, rec)
            odoo.post_note(task_id, f"<p>Frente de trabajo dibujado en campo{_by(editor)}.</p>")
            text = _norm(rec.get(F_OBS))
            if text and layer.has_field(F_SYNC_OBS):
                odoo.post_note(task_id, f"<p><b>Observación de campo</b>{_by(editor)}:</p>"
                                        f"<p>{_esc(text)}</p>")
                c.notes_posted += 1
                attrs[F_SYNC_OBS] = geo.text_fp(text)
            _link(layer, oid, task_id, attrs)
            c.created_in_odoo += 1
            logger.info("Frente creado en Odoo desde ArcGIS: task_id=%s (oid=%s)", task_id, oid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{layer.key} oid={oid}: {exc}")
            if task_id is None:
                _release(layer, oid)


# ================================================================== merge ===

def _odoo_location(t: dict) -> tuple[float, float] | None:
    lat, lon = t.get("partner_latitude"), t.get("partner_longitude")
    return (float(lat), float(lon)) if geo.valid_latlon(lat, lon) else None


def _merge_request_location(layer: ArcGISLayer, f: dict, t: dict,
                            upd: dict, c: LayerCounts) -> dict | None:
    """Merge de tres vías de la ubicación. Devuelve la geometría a escribir en ArcGIS, si hay."""
    arc = (f["_lat"], f["_lon"]) if geo.valid_latlon(f["_lat"], f["_lon"]) else None
    odo = _odoo_location(t)
    if arc is None and odo is None:
        return None
    base = geo.parse_point_fp(f.get(F_SYNC_GEOM)) if layer.has_field(F_SYNC_GEOM) else None
    if odo is None:
        decision = "to_odoo"      # Odoo sin coordenadas (o corruptas): completa campo
    elif arc is None:
        decision = "to_arcgis"
    else:
        decision = _three_way(arc, odo, base, _arcgis_ts(layer, f),
                              _odoo_ts(t.get("partner_write_date")), eq=geo.same_point)

    geometry = None
    final = arc
    if decision in ("to_odoo", "conflict_arcgis"):
        if t.get("partner_id"):
            odoo.update_partner_location(t["partner_id"], arc[0], arc[1])
            c.updated_in_odoo += 1
            if decision == "conflict_arcgis":
                c.conflicts += 1
            what = ("Ubicación tomada del mapa (Odoo no tenía coordenadas válidas)"
                    if odo is None else f"Ubicación corregida en campo{_by(_editor(layer, f))}")
            odoo.post_note(t["task_id"], f"<p>{what}: {arc[0]:.6f}, {arc[1]:.6f}</p>"
                                         + _map_links(*arc))
    elif decision in ("to_arcgis", "conflict_odoo"):
        if decision == "conflict_odoo":
            c.conflicts += 1
        final = odo
        geometry = {"x": odo[1], "y": odo[0], "spatialReference": {"wkid": 4326}}

    if decision != "none":
        _set(upd, layer, f, F_SYNC_GEOM, geo.point_fp(*final))
    _set(upd, layer, f, F_LAT, round(final[0], 7))
    _set(upd, layer, f, F_LON, round(final[1], 7))
    return geometry


def _new_request_feature(layer: ArcGISLayer, t: dict, loc: tuple[float, float]) -> dict:
    lat, lon = loc
    return {
        "attributes": {
            layer.id_field: t["task_id"],
            "name": t["description"] or "",
            "citizen_name": t["citizen_name"],
            "address": t["street"],
            "city": t["city"],
            "phone": t["phone"],
            "email": t["email"],
            F_STATUS: t["stage"],
            F_SYNC_STATUS: t["stage"],
            F_SYNC_GEOM: geo.point_fp(lat, lon),
            F_LAT: lat,
            F_LON: lon,
        },
        "geometry": {"x": lon, "y": lat, "spatialReference": {"wkid": 4326}},
    }


def _merge_requests(c: LayerCounts, errors: list[str]) -> None:
    layer = requests_layer
    tasks = odoo.get_project_tasks(settings.odoo_project_name)
    features: dict[int, dict] = {}
    for f in layer.linked_features():
        features.setdefault(f[layer.id_field], f)

    updates, adds = [], []
    for t in tasks:
        f = features.get(t["task_id"])
        if f is None:
            loc = _odoo_location(t)
            if loc is None:
                c.skipped_no_coordinates += 1
            else:
                adds.append(_new_request_feature(layer, t, loc))
            continue
        try:
            upd: dict[str, Any] = {}
            # 1) Datos que manda Odoo (ciudadano, contacto, descripción).
            for field, value in (("name", t["description"]), ("citizen_name", t["citizen_name"]),
                                 ("address", t["street"]), ("city", t["city"]),
                                 ("phone", t["phone"]), ("email", t["email"])):
                _set(upd, layer, f, field, value)
            # 2) Estado y ubicación: merge de tres vías.
            _merge_status(layer, f, t["task_id"], t["stage"], t["write_date"], upd, c)
            geometry = _merge_request_location(layer, f, t, upd, c)
            # 3) Observaciones de campo -> chatter.
            _merge_observations(layer, f, t["task_id"], upd, c)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{layer.key} task_id={t['task_id']}: {exc}")
            continue
        if upd or geometry:
            updates.append({"oid": f["_oid"], "attributes": upd, "geometry": geometry})

    ok, errs = layer.update_features(updates)
    c.updated_in_arcgis += ok
    errors.extend(errs)
    ok, errs = layer.add_features(adds)
    c.created_in_arcgis += ok
    errors.extend(errs)


def _merge_fronts(c: LayerCounts, errors: list[str]) -> None:
    layer = fronts_layer
    tasks = {t["task_id"]: t for t in odoo.get_workfront_tasks(settings.odoo_workfront_project_name)}
    updates = []
    for f in layer.linked_features():
        t = tasks.get(f[layer.id_field])
        if t is None:
            continue  # la reconciliación decide
        try:
            upd: dict[str, Any] = {}
            _merge_status(layer, f, t["task_id"], t["stage"], t["write_date"], upd, c)
            if layer.has_field(F_SYNC_FP):
                fp = geo.content_fp(_front_content(f))
                if fp != _norm(f.get(F_SYNC_FP)):
                    vals, length = _front_task_vals(f)
                    odoo.update_task(t["task_id"], vals)
                    c.updated_in_odoo += 1
                    _set(upd, layer, f, F_SYNC_FP, fp)
                    _set(upd, layer, f, F_LONGITUD, round(length, 1))
            _merge_observations(layer, f, t["task_id"], upd, c)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{layer.key} task_id={t['task_id']}: {exc}")
            continue
        if upd:
            updates.append({"oid": f["_oid"], "attributes": upd})
    ok, errs = layer.update_features(updates)
    c.updated_in_arcgis += ok
    errors.extend(errs)


# ============================================================ reconcile ====

def _reconcile(layer: ArcGISLayer, c: LayerCounts) -> None:
    """Borra features cuya tarea ya no existe en Odoo (Odoo manda en la existencia)."""
    linked = layer.query(
        f"{layer.id_field} IS NOT NULL AND {layer.id_field} <> {CLAIM_SENTINEL}",
        out_fields=[layer.oid_field(), layer.id_field], return_geometry=False,
    )
    existing = odoo.filter_existing_task_ids(list({f[layer.id_field] for f in linked}))
    orphans = [f["_oid"] for f in linked if f[layer.id_field] not in existing]
    if orphans:
        c.orphaned_deleted += layer.delete_features(orphans)
        logger.info("Reconciliación '%s': %s feature(s) huérfana(s) borrada(s).",
                    layer.key, len(orphans))


# ============================================================== assign =====

def _front_tag_name(front_task_id: int, front: dict) -> str:
    name = _norm(front.get(F_NOMBRE)) or f"ArcGIS #{front['_oid']}"
    return f"{FRONT_TAG_PREFIX}{front_task_id} · {name}"[:60]


def _assign(c_req: LayerCounts, c_fr: LayerCounts, errors: list[str]) -> int:
    """
    Asigna cada solicitud al frente NO cancelado más cercano a <= buffer m.
    - ArcGIS: campos frente_id / frente_nombre de la solicitud y
      solicitudes_asignadas del frente.
    - Odoo: etiqueta "Frente #<id> · <nombre>" en la tarea de la solicitud,
      y una nota en el frente cuando su conjunto de solicitudes cambia.
    Devuelve cuántas solicitudes cambiaron de frente.
    """
    req, fr_layer = requests_layer, fronts_layer
    buffer_m = settings.workfront_buffer_m
    cancelled = odoo.cancelled_label()

    all_fronts = fr_layer.linked_features()
    fronts = {f[fr_layer.id_field]: f for f in all_fronts
              if f.get("_geom") and _norm(f.get(F_STATUS)) != cancelled}

    # 1) Candidatos por consulta espacial en ArcGIS; distancia exacta en Python.
    best: dict[int, tuple[float, int]] = {}          # oid solicitud -> (dist, frente)
    for ftid, fr in fronts.items():
        for p in req.query_near(fr["_geom"], buffer_m * 1.5, where=f"{req.id_field} > 0"):
            if not geo.valid_latlon(p["_lat"], p["_lon"]):
                continue
            d = geo.point_to_polyline_m(p["_lat"], p["_lon"], fr["_geom"])
            if d <= buffer_m and (p["_oid"] not in best or (d, ftid) < best[p["_oid"]]):
                best[p["_oid"]] = (d, ftid)

    out_fields = [req.oid_field(), req.id_field] + [
        x for x in (F_FRENTE_ID, F_FRENTE_NOMBRE) if req.has_field(x)]
    points = req.query(f"{req.id_field} > 0", out_fields=out_fields, return_geometry=False)
    desired: dict[int, int] = {}                      # task solicitud -> task frente
    for p in points:
        if p["_oid"] in best:
            desired[p[req.id_field]] = best[p["_oid"]][1]

    # 2) Odoo: etiquetas por frente.
    tags = odoo.front_tags()
    tag_front = {tid: int(m.group(1)) for tid, name in tags.items()
                 if (m := _TAG_RE.match(name or ""))}
    front_tag: dict[int, int] = {}
    for ftid, fr in fronts.items():
        name = _front_tag_name(ftid, fr)
        existing = sorted(tid for tid, f_id in tag_front.items() if f_id == ftid)
        if existing:
            front_tag[ftid] = existing[0]
            if tags[existing[0]] != name:
                odoo.rename_tag(existing[0], name)
        else:
            front_tag[ftid] = odoo.create_tag(name, color=(ftid % 11) + 1)
    stale_tags = {tid for tid, f_id in tag_front.items() if f_id not in fronts}

    tasks = odoo.get_project_tasks(settings.odoo_project_name)
    names = {t["task_id"]: t["description"] for t in tasks}
    before: dict[int, set[int]] = defaultdict(set)
    after: dict[int, set[int]] = defaultdict(set)
    changes = 0
    for t in tasks:
        current = {tid for tid in t["tag_ids"] if tid in tag_front}
        for tid in current:
            before[tag_front[tid]].add(t["task_id"])
        target_front = desired.get(t["task_id"])
        if target_front:
            after[target_front].add(t["task_id"])
        target = {front_tag[target_front]} if target_front else set()
        add, remove = target - current, current - target - stale_tags
        if add or remove:
            try:
                odoo.change_task_tags(t["task_id"], add, remove)
                changes += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"asignación task_id={t['task_id']}: {exc}")

    # 3) ArcGIS: campos de asignación en solicitudes y conteo en frentes.
    updates = []
    for p in points:
        target_front = desired.get(p[req.id_field])
        front_name = None
        if target_front:
            front_name = (_norm(fronts[target_front].get(F_NOMBRE))
                          or f"{FRONT_TAG_PREFIX}{target_front}")
        upd: dict[str, Any] = {}
        _set(upd, req, p, F_FRENTE_ID, target_front)
        _set(upd, req, p, F_FRENTE_NOMBRE, front_name)
        if upd:
            updates.append({"oid": p["_oid"], "attributes": upd})
    ok, errs = req.update_features(updates)
    c_req.updated_in_arcgis += ok
    errors.extend(errs)

    front_updates = []
    for f in all_fronts:
        upd = {}
        _set(upd, fr_layer, f, F_ASIGNADAS, len(after.get(f[fr_layer.id_field], ())))
        if upd:
            front_updates.append({"oid": f["_oid"], "attributes": upd})
    ok, errs = fr_layer.update_features(front_updates)
    c_fr.updated_in_arcgis += ok
    errors.extend(errs)

    # 4) Nota en cada frente cuyo conjunto cambió.
    for ftid in fronts:
        added, removed = after[ftid] - before[ftid], before[ftid] - after[ftid]
        if not (added or removed):
            continue
        def fmt(ids: set[int]) -> str:
            return ", ".join(f"#{i} {_esc(names.get(i, ''))}" for i in sorted(ids))
        body = (f"<p><b>Solicitudes asignadas por cercanía</b> (≤ {buffer_m:g} m): "
                f"{len(after[ftid])}</p>")
        if added:
            body += f"<p>Nuevas: {fmt(added)}</p>"
        if removed:
            body += f"<p>Retiradas: {fmt(removed)}</p>"
        try:
            odoo.post_note(ftid, body)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"nota frente {ftid}: {exc}")

    # 5) Etiquetas de frentes que ya no existen o se cancelaron.
    if stale_tags:
        odoo.delete_tags(stale_tags)
    return changes


# ========================================================== attachments ====

def _sync_attachments(layer: ArcGISLayer, c: LayerCounts, errors: list[str]) -> None:
    rows = layer.linked_attachments()
    if not rows:
        return
    linked = {f["_oid"]: f[layer.id_field] for f in layer.query(
        f"{layer.id_field} > 0", out_fields=[layer.oid_field(), layer.id_field],
        return_geometry=False)}
    synced = odoo.synced_attachment_keys({linked[r["parent_oid"]] for r in rows
                                          if r["parent_oid"] in linked})
    for r in rows:
        task_id = linked.get(r["parent_oid"])
        if not task_id:
            continue
        # Clave estable del adjunto: su GlobalID, o "oidPadre-idAdjunto".
        fallback = "{}-{}".format(r["parent_oid"], r["id"])
        key = "arcgis:{}:{}".format(layer.key, r.get("global_id") or fallback)
        if key in synced:
            continue
        if r.get("size", 0) > MAX_ATTACHMENT_BYTES:
            _warn_once(key, f"Adjunto {r['name']} ({r['size']} bytes) supera el máximo; no se copia.")
            continue
        try:
            name, data = layer.download_attachment(r["parent_oid"], r["id"])
            att_id = odoo.attach_file(task_id, r.get("name") or name, data,
                                      r.get("content_type"), key)
            odoo.post_note(task_id, f"<p><b>Adjunto de campo</b>: {_esc(r.get('name') or name)}</p>",
                           attachment_ids=[att_id])
            c.photos_synced += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{layer.key} adjunto {r.get('name')}: {exc}")


# ============================================================ pipeline =====

def _guard(errors: list[str], label: str, fn: Callable, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falló el paso %s", label)
        errors.append(f"{label}: {exc}")
        return None


def run_pipeline(reason: str = "manual", steps: tuple[str, ...] = ALL_STEPS,
                 blocking: bool = True) -> PipelineSummary | None:
    """Una pasada completa (o los pasos indicados). Nunca corren dos a la vez."""
    if not _run_lock.acquire(blocking=blocking):
        logger.info("Ya hay una pasada en curso; se omite (%s).", reason)
        return None
    try:
        return _run(reason, steps)
    finally:
        _run_lock.release()


def _run(reason: str, steps: tuple[str, ...]) -> PipelineSummary:
    global _last_summary
    started, t0 = datetime.now(timezone.utc), time.monotonic()
    req_c, fr_c = LayerCounts(), LayerCounts()
    errors: list[str] = []
    assignment_changes = 0

    if not requests_layer.configured:
        errors.append("ARCGIS_FEATURE_LAYER_ITEM_ID no está configurado.")
    fronts_on = fronts_layer.configured
    layers = [(requests_layer, req_c)] if requests_layer.configured else []
    if fronts_on:
        layers.append((fronts_layer, fr_c))

    if layers:
        _guard(errors, "schema solicitudes", _check_schema, requests_layer,
               (F_SYNC_STATUS, F_SYNC_GEOM, F_SYNC_OBS, F_OBS))
        if fronts_on:
            _guard(errors, "schema frentes", _check_schema, fronts_layer,
                   (F_SYNC_STATUS, F_SYNC_FP, F_SYNC_OBS, F_OBS))

    if "reverse" in steps:
        if requests_layer.configured:
            _guard(errors, "reverse solicitudes", _reverse_requests, req_c, errors)
        if fronts_on:
            _guard(errors, "reverse frentes", _reverse_fronts, fr_c, errors)
    if "merge" in steps:
        if requests_layer.configured:
            _guard(errors, "merge solicitudes", _merge_requests, req_c, errors)
        if fronts_on:
            _guard(errors, "merge frentes", _merge_fronts, fr_c, errors)
    if "reconcile" in steps:
        for layer, counts in layers:
            _guard(errors, f"reconcile {layer.key}", _reconcile, layer, counts)
    if "assign" in steps and fronts_on and requests_layer.configured:
        assignment_changes = _guard(errors, "asignación", _assign, req_c, fr_c, errors) or 0
    if "attachments" in steps:
        for layer, counts in layers:
            _guard(errors, f"adjuntos {layer.key}", _sync_attachments, layer, counts, errors)

    summary = PipelineSummary(
        ok=not errors, reason=reason, steps=list(steps), started_at=started,
        duration_s=round(time.monotonic() - t0, 2), solicitudes=req_c, frentes=fr_c,
        assignment_changes=assignment_changes, errors=errors,
    )
    _last_summary = summary
    logger.info("Pasada '%s' en %.1fs · solicitudes=%s · frentes=%s · asignación=%s · errores=%s",
                reason, summary.duration_s, _compact(req_c), _compact(fr_c),
                assignment_changes, len(errors))
    return summary


def _compact(c: LayerCounts) -> dict[str, int]:
    return {k: v for k, v in c.model_dump().items() if v}


def get_last_summary() -> PipelineSummary | None:
    return _last_summary


# ============================================================== runner =====

class _Runner:
    """
    Recibe "timbrazos" (webhooks) y los agrupa: espera
    WEBHOOK_DEBOUNCE_SECONDS, corre UNA pasada con todos los motivos
    acumulados, y repite mientras sigan llegando. Así una ráfaga de 20
    eventos produce 1-2 pasadas, no 20, y la espera deja que Odoo confirme
    su transacción antes de que la leamos.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._worker: threading.Thread | None = None
        self._busy = False

    def request(self, reason: str) -> None:
        with self._lock:
            self._pending.add(reason)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="sync-runner", daemon=True)
                self._worker.start()

    def _loop(self) -> None:
        while True:
            time.sleep(max(0.0, settings.webhook_debounce_seconds))
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                reasons = sorted(self._pending)
                self._pending.clear()
                self._busy = True
            try:
                run_pipeline(reason="webhook: " + ", ".join(reasons))
            except Exception:  # noqa: BLE001
                logger.exception("Falló la pasada disparada por webhook")
            finally:
                with self._lock:
                    self._busy = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._busy or _run_lock.locked(),
                    "pending_reasons": sorted(self._pending)}


runner = _Runner()


# ================================================ simulación manual (curl) ===

def handle_manual_status(odoo_task_id: int, new_status: str | None, note: str | None) -> None:
    """
    /webhook/arcgis con payload propio {odoo_task_id, new_status, note}:
    útil para demostrar el flujo sin tocar el mapa. Escribe en Odoo y pide
    una pasada para que el cambio llegue también a la capa.
    """
    parts = []
    if new_status:
        if odoo.set_task_state_by_label(odoo_task_id, new_status):
            parts.append(f"Estado actualizado desde ArcGIS: <b>{_esc(new_status)}</b>")
        else:
            parts.append(f"El estado <b>{_esc(new_status)}</b> no existe en Odoo; "
                         f"se registra solo como nota.")
    if note:
        parts.append(_esc(note))
    odoo.post_note(odoo_task_id, "<p>" + "<br/>".join(parts or ["Actualización recibida desde ArcGIS."]) + "</p>")
    runner.request("simulación /webhook/arcgis")