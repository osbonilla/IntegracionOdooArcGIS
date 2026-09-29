"""
Configuración del microservicio, leída de variables de entorno / `.env`.

`extra="ignore"`: si el `.env` trae variables que este archivo no declara
(por ejemplo las de Postgres/Odoo del docker-compose raíz), se ignoran en
vez de tumbar el arranque con "Extra inputs are not permitted".
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Odoo -------------------------------------------------------------
    odoo_url: str = "http://odoo:8069"
    odoo_db: str = "odoo_demo"
    odoo_username: str = "admin"
    odoo_password: str = ""
    # Proyecto de las solicitudes ciudadanas (puntos).
    odoo_project_name: str = "Solicitudes Ciudadanas"
    # Proyecto de los frentes de trabajo (líneas dibujadas en Field Maps).
    odoo_workfront_project_name: str = "Frentes de Trabajo"

    # --- ArcGIS -----------------------------------------------------------
    arcgis_url: str = "https://www.arcgis.com"
    arcgis_verify_cert: bool = True
    arcgis_username: str | None = None
    arcgis_password: str | None = None
    arcgis_client_id: str | None = None
    arcgis_client_secret: str | None = None
    # Capa de puntos (solicitudes). Nombre histórico de la variable.
    arcgis_feature_layer_item_id: str | None = None
    # Capa de líneas (frentes de trabajo). Vacío = frentes desactivados.
    arcgis_workfront_layer_item_id: str | None = None
    # Mapa web que usa Field Maps. Opcional: si está, las tareas de Odoo
    # llevan enlaces "Ver en el mapa" / "Abrir en Field Maps".
    arcgis_webmap_id: str | None = None

    # --- Webhooks -----------------------------------------------------------
    # Clave de firma configurada en el webhook de ArcGIS (HMAC-SHA256).
    # Vacío = no se verifica la firma.
    arcgis_webhook_secret: str | None = None
    # Espera antes de procesar un webhook. Cubre dos cosas: agrupa ráfagas
    # de eventos, y deja que Odoo haga commit (Odoo llama al webhook ANTES
    # de confirmar la transacción, con timeout de 1 s).
    webhook_debounce_seconds: float = 3.0
    # ArcGIS Online puede avisar ANTES de que el registro nuevo sea visible en
    # las consultas: tras cada webhook de ArcGIS se repite la pasada a estos
    # segundos. Vacío = sin re-chequeos (solo scheduler como respaldo).
    webhook_recheck_seconds: str = "10,30,60"
    # Si se define, /sync/* exige la cabecera X-API-Key y /webhook/odoo
    # exige ?token=... (recomendado cuando la API se expone con un túnel).
    integration_api_key: str | None = None

    # --- Frentes de trabajo ------------------------------------------------
    # Distancia máxima (m) entre una solicitud y un frente para asignarla.
    workfront_buffer_m: float = 50.0

    # --- Scheduler -----------------------------------------------------------
    # Pasada completa periódica (red de seguridad si se pierde un webhook).
    # 0 = desactivado.
    sync_interval_minutes: int = 5


settings = Settings()