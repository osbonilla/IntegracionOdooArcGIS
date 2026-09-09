# Demo: Integración Odoo ↔ ArcGIS (Online / Enterprise)

Proyecto de demostración técnica para evidenciar cómo un sistema empresarial
(ERP) como **Odoo** puede integrarse con **ArcGIS Online (AGOL)** o
**ArcGIS Enterprise**, pensado para presentar a **municipios que ya operan
Odoo** y quieren sumarle capacidades de análisis y visualización espacial.

> Objetivo del demo: mostrar que la información de negocio que ya vive en
> Odoo (contactos, solicitudes, activos) puede convertirse en un **mapa
> vivo, analizable y compartible** en ArcGIS, sin reemplazar el ERP ni
> duplicar procesos.

---

## 1. Narrativa del demo

Escenario: un municipio usa Odoo para registrar **solicitudes ciudadanas**
(baches, alumbrado, fugas de agua, poda de árboles, etc.) como contactos
(`res.partner`) geolocalizados y etiquetados con el tag `Solicitud Ciudadana`.

```text
Ciudadano reporta un problema
        │
        ▼
   ODOO (res.partner + tag + lat/lon)
        │
        ▼
 Integration API (FastAPI) ── sincroniza cada N minutos o bajo demanda
        │
        ▼
 ArcGIS Online / Enterprise (Hosted Feature Layer)
        │
        ▼
 Web Map / Dashboard ArcGIS
        │
        ▼
Cuadrillas de campo, autoridades, ciudadanía (mapa público)
```

Y el flujo inverso, para mostrar que **no es un mapa estático**:

```text
Operario marca "Resuelto" en ArcGIS (Field Maps / Dashboard)
        │
        ▼
Webhook de ArcGIS -> Integration API
        │
        ▼
Nota registrada en el chatter del contacto en Odoo
```

Este segundo flujo es la pieza clave para el pitch a municipios: **el mapa
y el ERP se retroalimentan**, no son sistemas aislados.

> Nota: para simplificar el despliegue, se usa `res.partner` (contactos)
> en vez de un modelo custom, aprovechando que Odoo Community ya trae
> `partner_latitude` / `partner_longitude` (módulo `base_geolocalize`).
> En una implementación real se recomienda un modelo de negocio propio
> (`project.task`, `helpdesk.ticket` o un módulo custom `municipio.solicitud`)
> — ver sección [10. Roadmap](#10-roadmap-hacia-una-solución-productiva).

---

## 2. Arquitectura

```text
                         INTERNET
                            │
                            ▼
        ┌───────────────────────────────────┐
        │        docker-compose (local)      │
        │                                     │
        │   ┌──────────┐     ┌─────────────┐ │        ┌───────────────────┐
        │   │ Postgres │◄───►│    Odoo     │ │        │  ArcGIS Online /   │
        │   │  (odoo)  │     │  (18.0)     │ │        │  ArcGIS Enterprise │
        │   └──────────┘     └──────┬──────┘ │        │                    │
        │                           │ XML-RPC │        │  Hosted Feature    │
        │                    ┌──────▼──────┐  │  REST  │  Layer             │
        │                    │ Integration │──┼───────►│  (arcgis Python    │
        │                    │ API (FastAPI)│  │  API   │   API / REST)     │
        │                    └──────┬──────┘  │        └─────────┬──────────┘
        │                           │webhook   │                  │
        │                    (recibe POST) ◄───┼──────────────────┘
        └───────────────────────────────────┘                     │
                                                                     ▼
                                                          Web Map / Dashboard
                                                          / Experience Builder
```

**Componentes:**

| Componente         | Rol                                                                 |
|--------------------|----------------------------------------------------------------------|
| `db`               | PostgreSQL exclusivo para el motor de Odoo                           |
| `odoo`             | Instancia Odoo Community 18.0 (fuente de verdad del negocio)         |
| `integration-api`  | Microservicio FastAPI: traduce Odoo ↔ ArcGIS, expone endpoints REST  |
| ArcGIS (externo)    | AGOL o Enterprise — no corre en Docker, se conecta vía API/REST      |

No se despliega ArcGIS Enterprise en Docker (requiere licenciamiento e
infraestructura propia de Esri). El microservicio está escrito para
conectarse indistintamente a **AGOL** o a **Enterprise** cambiando una sola
variable de entorno (`ARCGIS_URL`).

---

## 3. Estructura del proyecto

```text
odoo-arcgis-demo/
├── docker-compose.yml
├── .env.example                  # credenciales de la BD de Odoo
├── odoo/
│   ├── config/odoo.conf
│   └── addons/                   # para módulos custom futuros
├── integration-api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env.example              # credenciales Odoo + ArcGIS
│   └── app/
│       ├── main.py               # FastAPI: endpoints + scheduler
│       ├── config.py             # settings (pydantic-settings)
│       ├── schemas.py            # modelos Pydantic
│       ├── odoo_client.py        # cliente XML-RPC hacia Odoo
│       ├── arcgis_client.py      # cliente ArcGIS API for Python
│       └── sync.py               # orquestación del sync bidireccional
├── scripts/
│   ├── requirements.txt
│   ├── seed_odoo_demo_data.py    # crea contactos demo geolocalizados
│   └── create_arcgis_feature_layer.py  # crea la Hosted Feature Layer
└── README.md
```

---

## 4. Prerrequisitos

- Docker y Docker Compose v2
- Una cuenta **ArcGIS Online** (developer/trial sirve) **o** acceso a un
  **ArcGIS Enterprise** con permisos para publicar Hosted Feature Layers
- Python 3.10+ en tu máquina (solo para correr los scripts de `scripts/`,
  fuera de Docker)
- Puertos libres: `8069` (Odoo), `8000` (Integration API)

---

## 5. Puesta en marcha paso a paso

### 5.1. Levantar Odoo + Postgres

```bash
cp .env.example .env
# editar .env si quieres cambiar el password de postgres

docker compose up -d db odoo
docker compose logs -f odoo   # esperar "HTTP service (werkzeug) running"
```

Abrir `http://localhost:8069`, crear la base de datos:

- **Nombre de base de datos:** `odoo_demo` (debe coincidir con `ODOO_DB`
  en `integration-api/.env` y con `dbfilter` en `odoo/config/odoo.conf`)
- Usuario/contraseña admin a tu elección (actualiza `ODOO_USERNAME` /
  `ODOO_PASSWORD` en `integration-api/.env` acorde)

Activa el módulo **Contactos** (viene instalado por defecto) — no se
requiere instalar módulos adicionales para esta demo mínima.

### 5.2. Crear la Hosted Feature Layer en ArcGIS

```bash
cd scripts
python -m venv .venv && source .venv/bin/activate   # o el equivalente en Windows
pip install -r requirements.txt

cp ../integration-api/.env.example .env
# editar .env con tus credenciales reales de ArcGIS y Odoo

python create_arcgis_feature_layer.py
```

Copia el `Item ID` que imprime el script.

### 5.3. Configurar y levantar el microservicio de integración

```bash
cd ../integration-api
cp .env.example .env
# completar ODOO_* (según el paso 5.1) y ARCGIS_* (incluyendo el Item ID del paso 5.2)

cd ..
docker compose up -d --build integration-api
docker compose logs -f integration-api
```

Verifica: `http://localhost:8000/health` → `{"status": "ok"}`
Documentación interactiva (Swagger): `http://localhost:8000/docs`

### 5.4. Poblar datos demo en Odoo

```bash
cd scripts
python seed_odoo_demo_data.py
```

Esto crea 8 "solicitudes ciudadanas" geolocalizadas (por defecto en Quito;
edita las coordenadas en el script para tu municipio).

### 5.5. Ejecutar la sincronización

```bash
curl -X POST http://localhost:8000/sync/run
```

Respuesta esperada:

```json
{
  "ok": true,
  "fetched_from_odoo": 8,
  "created_in_arcgis": 8,
  "updated_in_arcgis": 0,
  "skipped_no_coordinates": 0,
  "errors": []
}
```

Abre la capa en ArcGIS Online (Content → busca el título de la capa) y
crea un **Web Map** o **Dashboard** encima para la presentación visual.

### 5.6. (Opcional) Sincronización automática

En `integration-api/.env`, define `SYNC_INTERVAL_MINUTES=5` (por ejemplo)
y reinicia el contenedor: `docker compose restart integration-api`.

---

## 6. Flujo inverso: ArcGIS → Odoo

Para la demo de bidireccionalidad, expón `POST /webhook/arcgis` (ver
`app/schemas.py::ArcGISWebhookPayload`) y configúralo como **Webhook** de
la Feature Layer en ArcGIS Online (Content → capa → Settings → Webhooks →
"FeaturesUpdated"). Como AGOL entrega su propio formato de payload, en un
entorno real se agrega una función `adapt_agol_payload()` en `sync.py`
para mapear el JSON nativo de AGOL a `ArcGISWebhookPayload`. En esta demo,
para simplificar la presentación en vivo, se puede simular el webhook
directamente:

```bash
curl -X POST http://localhost:8000/webhook/arcgis \
  -H "Content-Type: application/json" \
  -d '{"odoo_partner_id": 12, "new_status": "Resuelto", "note": "Cuadrilla atendió el reporte"}'
```

Verifica en Odoo → Contactos → el contacto correspondiente → pestaña de
mensajes/chatter: aparecerá la nota.

---

## 7. AGOL vs. Enterprise — qué cambia

| Aspecto                     | ArcGIS Online                          | ArcGIS Enterprise                                   |
|------------------------------|-----------------------------------------|-------------------------------------------------------|
| `ARCGIS_URL`                 | `https://www.arcgis.com`               | URL del Web Adaptor de Portal (`https://host/portal`) |
| Autenticación recomendada    | OAuth 2.0 (App registrada) o built-in  | OAuth 2.0, IWA o PKI, según configuración del Portal  |
| Publicación de Feature Layer | Hosting automático en AGOL              | Requiere Hosting Server federado configurado          |
| Certificados                 | Gestionados por Esri                   | Responsabilidad del municipio (SSL/TLS propio)        |
| Alcance típico en municipios | Pilotos, POCs, dependencias pequeñas   | Datos sensibles, alta disponibilidad, on-premise      |

El código de `arcgis_client.py` **no cambia** entre ambos escenarios: el
paquete `arcgis` (ArcGIS API for Python) abstrae la diferencia. Solo
cambian la URL y el método de autenticación.

---

## 8. Seguridad

- **Nunca** commitear los archivos `.env` reales (ya excluidos vía
  `.gitignore`); usar `.env.example` como plantilla.
- Esta demo usa usuario/password simple contra ArcGIS por simplicidad.
  **Para producción**, migrar a OAuth 2.0:
  - AGOL: registrar una aplicación en el portal del desarrollador y usar
    `client_id` + `client_secret` con `GIS(url, client_id=..., client_secret=...)`.
  - Enterprise: evaluar IWA (Windows Integrated Auth) o certificados PKI
    si el municipio ya tiene esa infraestructura.
- El `admin_passwd` de `odoo/config/odoo.conf` es solo para desarrollo
  local — cambiarlo y nunca exponerlo si el contenedor sale a internet.
- `integration-api` no debe exponerse públicamente sin autenticación en el
  endpoint `/webhook/arcgis` (agregar validación de secreto compartido o
  restringir por IP de origen de Esri en un entorno real).
- Si Odoo se expone fuera de la red local, usar Nginx como reverse proxy
  con TLS (no cubierto en esta demo local, ver sección de roadmap).

---

## 9. Troubleshooting

| Síntoma | Causa probable | Verificación |
|---|---|---|
| `integration-api` no arranca | Falta `.env` o falta `ARCGIS_FEATURE_LAYER_ITEM_ID` | `docker compose logs integration-api` |
| Error de autenticación con Odoo | `ODOO_DB` no coincide con la BD creada, o password incorrecto | Probar login manual en `http://localhost:8069` |
| `/sync/run` devuelve `skipped_no_coordinates` alto | Los contactos no tienen `partner_latitude/longitude` | Revisar en Odoo → Contactos → pestaña "Ventas y Compras" o el campo de geolocalización |
| Error `arcgis` al conectar | Credenciales incorrectas o URL de Enterprise sin Web Adaptor correcto | `python -c "from arcgis.gis import GIS; GIS(url, user, pwd)"` desde una shell local |
| Feature Layer no se actualiza pero no hay error | El `Item ID` en `.env` apunta a otra capa | Confirmar el Item ID impreso por `create_arcgis_feature_layer.py` |
| Contenedor `odoo` reinicia en loop | Conflicto de `dbfilter` en `odoo.conf` con el nombre real de la BD | Ajustar `dbfilter` en `odoo/config/odoo.conf` |

Comandos generales de diagnóstico:

```bash
docker compose ps
docker compose logs -f odoo
docker compose logs -f integration-api
curl -s http://localhost:8000/health
curl -s http://localhost:8000/sync/last
```

---

## 10. Roadmap hacia una solución productiva

Este demo está deliberadamente simplificado para que se pueda montar en
menos de una hora. Para llevarlo a un piloto real con un municipio:

1. **Modelo de negocio propio en Odoo** en vez de `res.partner`:
   módulo custom `municipio_solicitudes` con estados, SLA, adjuntos (fotos)
   y relación a `project.task` para asignación de cuadrillas.
2. **OAuth 2.0** real en ambos extremos (Odoo vía `oauth2` module /
   API keys, ArcGIS vía App Registration).
3. **Idempotencia robusta**: mover el matching Odoo↔ArcGIS de `OBJECTID`
   consultado en cada request a un campo indexado único + `upsert` batch
   (`edit_features` soporta arrays; hoy se procesa 1 a 1 por claridad).
4. **Cola de mensajes** (RabbitMQ/Redis) si el volumen de solicitudes
   crece, en vez de sync por polling/cron.
5. **Adaptador real de webhook AGOL** (`adapt_agol_payload()`), ya que el
   payload nativo de Esri difiere del simplificado usado aquí.
6. **Nginx + TLS** como reverse proxy delante de Odoo e Integration API
   si se expone fuera de la red interna del municipio.
7. **Dashboard ArcGIS público** de solo lectura para transparencia
   ciudadana, separado del Web Map operativo interno.
8. **Métricas y logging centralizado** (ej. envío a un stack ELK o
   Grafana Loki) para operar el puente en producción.

---

## 11. Licencia y alcance

Proyecto de demostración técnica, sin garantías, pensado para pruebas de
concepto comerciales/técnicas. Antes de un despliegue productivo, validar
licenciamiento de ArcGIS Enterprise/Online con Esri y de Odoo Enterprise
si aplica (este demo usa Odoo Community, LGPL).
