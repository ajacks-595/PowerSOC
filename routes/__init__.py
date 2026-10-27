"""PowerSOC web layer, split out of app.py (2026-10-07).

Importing this package registers every route: routes.admin adds its handlers
to routes.api.api_bp as an import side effect, so endpoint names stay
`api.*` / `pages.*` / `home.*` (auth.PUBLIC_ENDPOINTS relies on them).
"""
from routes import admin  # noqa: F401  (registers admin routes on api_bp)
from routes.api import api_bp
from routes.home import home_bp
from routes.metrics import metrics_bp
from routes.pages import pages_bp

BLUEPRINTS = (pages_bp, metrics_bp, api_bp, home_bp)
