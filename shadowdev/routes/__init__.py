"""Every HTTP route: (method, path) -> Route (its handler, and whether anyone may call it and how often).
One module per area of the site."""
from . import auth, projects, settings, webhook

ROUTES = {**auth.ROUTES, **settings.ROUTES, **projects.ROUTES, **webhook.ROUTES}

# Requests per client and minute, by path, for the routes that have a limit: the rate limiter's table.
LIMITS = {path: route.limit for (_, path), route in ROUTES.items() if route.limit is not None}
