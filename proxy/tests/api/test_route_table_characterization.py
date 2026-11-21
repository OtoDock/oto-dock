"""Characterization of the app's route table: every route's path, methods,
endpoint name and kind, in the order the app matches them, the included
routers walked through. A module split moves a route's function to another
module and the table stays the same, so a router that was not included any
more, or included in another order, shows here."""

from tests.fixtures.characterization.golden_master import check_golden


def _walk(routes, prefix: str, table: list, low: bool = False) -> None:
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            context = getattr(route, "include_context", None)
            inner = prefix + (getattr(context, "prefix", "") or "")
            _walk(nested.routes, inner, table)
            _walk(getattr(nested, "_low_priority_routes", []), inner, table, low=True)
            continue
        if getattr(route, "name", "") == "dashboard-assets":
            continue  # mounted only when the dashboard is built
        endpoint = getattr(route, "endpoint", None)
        table.append({
            "path": prefix + getattr(route, "path", ""),
            "methods": sorted(getattr(route, "methods", None) or []),
            "name": getattr(endpoint, "__name__", type(route).__name__),
            "kind": type(route).__name__ + (" (low priority)" if low else ""),
        })


def test_the_route_table():
    from app import app

    table: list = []
    _walk(app.routes, "", table)
    check_golden("route-table", table)
