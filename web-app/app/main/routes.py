"""
Routes principales de l'application.
"""

from flask import render_template
from app.main import bp


@bp.route('/')
def index():
    """Page principale de l'application."""
    return render_template('index.html')