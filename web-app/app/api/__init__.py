"""
Blueprint API pour les routes REST.
"""

from flask import Blueprint

bp = Blueprint('api', __name__)

# Import des routes après la création du blueprint
from . import routes  # noqa: E402,F401
from . import analysis_routes  # noqa: E402,F401
from . import editor_routes  # noqa: E402,F401
from . import activities_routes  # noqa: E402,F401
from . import dashboard_routes  # noqa: E402,F401
from . import segments_routes  # noqa: E402,F401
from . import users_routes  # noqa: E402,F401
from . import profile_routes  # noqa: E402,F401
from . import invitations_routes  # noqa: E402,F401
from . import map_routes  # noqa: E402,F401
from . import mfa_routes  # noqa: E402,F401
from . import admin_routes  # noqa: E402,F401
from . import account_routes  # noqa: E402,F401
from . import route_builder_routes  # noqa: E402,F401
