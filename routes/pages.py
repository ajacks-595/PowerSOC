"""Server-rendered pages (dashboard, history, flow, diagnostics, settings) and
the root-scoped service worker."""
from __future__ import annotations

import logging

from flask import Blueprint, render_template

import config

log = logging.getLogger("powersoc.pages")


# ---------- page routes ---------------------------------------------------

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/")
def dashboard():
    return render_template("dashboard.html", theme=config.DEFAULT_THEME,
                           active="dashboard")


@pages_bp.route("/history")
def history():
    return render_template("history.html", theme=config.DEFAULT_THEME,
                           active="history")


@pages_bp.route("/flow")
def flow():
    return render_template("flow.html", theme=config.DEFAULT_THEME, active="flow")


@pages_bp.route("/indepth")
def indepth():
    return render_template("indepth.html", theme=config.DEFAULT_THEME,
                           active="indepth")


@pages_bp.route("/data")
def data():
    return render_template("data.html", theme=config.DEFAULT_THEME, active="data")


@pages_bp.route("/settings")
def settings():
    return render_template("settings.html", theme=config.DEFAULT_THEME,
                           active="settings")


@pages_bp.route("/sw.js")
def service_worker():
    """Serve the service worker from root scope (so it can control '/'), with
    the cache version injected from APP_VERSION so deploys bust stale caches."""
    import os

    from flask import Response, current_app
    with open(os.path.join(current_app.static_folder, "sw.js")) as f:
        js = f.read().replace("__VERSION__", config.APP_VERSION)
    resp = Response(js, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp
