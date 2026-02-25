import pytest


def _setup_server_module():
    # Lazy import of fixtures to setup GIMP mocks before importing the server module
    from tests import test_fixtures as fixtures

    fixtures.setup_gimp_mocks()
    import gimp_mcp_server as server

    return fixtures, server


def test_discovery_list_gegl_filters():
    fixtures, server = _setup_server_module()
    # Discovery call should return a structured list of filters
    result = server.list_gegl_filters(None, None)
    assert isinstance(result, dict)
    assert "available_filters" in result
    filters = result["available_filters"]
    names = [f.get("name") for f in filters]
    assert "gaussian_blur" in names
    # Validate shape of a sample entry
    first = next((f for f in filters if f.get("name") == "gaussian_blur"), None)
    assert first is not None
    assert isinstance(first.get("parameters"), list)
    for p in first["parameters"]:
        assert "name" in p and "type" in p


def test_apply_gegl_filter(monkeypatch):
    fixtures, server = _setup_server_module()

    class DummyConn:
        def __init__(self):
            pass

        def send_command(self, command_type, params=None):
            if command_type != "exec":
                return {"status": "error", "error": "unexpected"}
            cmds = params.get("cmds", []) if params else []
            # If the payload contains a preview flag, simulate preview path
            if any("preview" in c for c in cmds):
                return {"status": "preview", "results": {}}
            return {"status": "success", "results": {"applied": True}}

    monkeypatch.setattr(server, "get_gimp_connection", lambda: DummyConn())

    result = server.apply_gegl_filter(None, "gaussian_blur", {"radius": 5})
    assert isinstance(result, dict)
    assert result.get("applied") is True


def test_preview_gegl_filter(monkeypatch):
    fixtures, server = _setup_server_module()

    class DummyConn:
        def __init__(self):
            pass

        def send_command(self, command_type, params=None):
            if command_type != "exec":
                return {"status": "error", "error": "unexpected"}
            cmds = params.get("cmds", []) if params else []
            if any("preview" in c for c in cmds):
                return {"status": "preview", "results": {}}
            return {"status": "success", "results": {}}

    monkeypatch.setattr(server, "get_gimp_connection", lambda: DummyConn())

    result = server.preview_gegl_filter(None, "gaussian_blur", {"radius": 3})
    assert isinstance(result, dict)
    assert result.get("status") == "success"
    assert "preview" in result
