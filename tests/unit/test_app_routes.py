from tf_api.main import app


def test_api_metrics_route_precedes_spa_catch_all():
    routes = [route.path for route in app.routes]

    metrics_index = routes.index("/api/admin/metrics")
    catch_all_index = routes.index("/{full_path:path}")

    assert metrics_index < catch_all_index
