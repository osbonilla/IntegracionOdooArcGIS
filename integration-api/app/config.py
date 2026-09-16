from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Odoo
    odoo_url: str = "http://odoo:8069"
    odoo_db: str = "odoo_demo"
    odoo_username: str = "admin"
    odoo_password: str = "admin"
    odoo_project_name: str = "Solicitudes Ciudadanas"

    # ArcGIS
    arcgis_url: str = "https://www.arcgis.com"
    arcgis_verify_cert: bool = True
    arcgis_username: str = ""
    arcgis_password: str = ""
    arcgis_client_id: str = ""
    arcgis_client_secret: str = ""
    arcgis_feature_layer_item_id: str = ""

    # Sync
    sync_interval_minutes: int = 0

    class Config:
        env_file = ".env"


settings = Settings()
