"""Flask app definition"""

from flask import Flask, request
from app.routes.api import api as api_blueprint
from app.routes.index import IndexPage


def create_app():
    """Create the Flask app with index route"""
    app = Flask(__name__)
    app.register_blueprint(api_blueprint, url_prefix="/api")

    @app.route("/", methods=["GET", "POST"])
    def index():
        index_page = IndexPage(request)
        return index_page.template

    return app
