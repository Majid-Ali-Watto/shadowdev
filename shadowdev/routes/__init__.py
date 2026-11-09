"""Every HTTP route: (method, path) -> function(request). One module per area of the site."""
from . import auth, projects, settings, webhook

ROUTES = {**auth.ROUTES, **settings.ROUTES, **projects.ROUTES, **webhook.ROUTES}
