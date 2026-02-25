import unittest
from unittest.mock import patch


def _setup_server_module():
    # Lazy import of fixtures to setup GIMP mocks before importing the server module
    from tests import test_fixtures as fixtures

    fixtures.setup_gimp_mocks()
    import gimp_mcp_server as server

    return fixtures, server


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.fixtures, self.server = _setup_server_module()

    def test_discovery_list_gegl_filters(self):
        # Discovery call should return a structured list of filters
        result = self.server.list_gegl_filters(None, None)
        self.assertIsInstance(result, dict)
        self.assertIn("available_filters", result)
        filters = result["available_filters"]
        names = [f.get("name") for f in filters]
        self.assertIn("gaussian_blur", names)
        # Validate shape of a sample entry
        first = next((f for f in filters if f.get("name") == "gaussian_blur"), None)
        self.assertIsNotNone(first)
        self.assertIsInstance(first.get("parameters"), list)
        for p in first["parameters"]:
            self.assertIn("name", p)
            self.assertIn("type", p)

    def test_apply_gegl_filter(self):
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

        with patch.object(self.server, "get_gimp_connection", return_value=DummyConn()):
            result = self.server.apply_gegl_filter(None, "gaussian_blur", {"radius": 5})
            self.assertIsInstance(result, dict)
            self.assertTrue(result.get("applied"))

    def test_preview_gegl_filter(self):
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

        with patch.object(self.server, "get_gimp_connection", return_value=DummyConn()):
            result = self.server.preview_gegl_filter(
                None, "gaussian_blur", {"radius": 3}
            )
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("status"), "success")
            self.assertIn("preview", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
