import unittest
import importlib
import sys
import types
from tests.test_fixtures import setup_gimp_mocks, cleanup_gimp_mocks, create_mock_plugin


class TestListFilters(unittest.TestCase):
    def setUp(self):
        setup_gimp_mocks()
        self.plugin = create_mock_plugin()
        # Ensure the gegl discovery module is importable
        import gimp_mcp_server  # noqa: F401

    def tearDown(self):
        cleanup_gimp_mocks()

    def _call_discovery(self, filter_type=None):
        # Access the underlying implementation if wrapped by the MCP decorator
        fn = getattr(__import__("gimp_mcp_server"), "list_gegl_filters")
        if hasattr(fn, "__wrapped__"):
            return fn.__wrapped__(None, filter_type)
        return fn(None, filter_type)

    def test_list_gegl_filters_discovery(self):
        result = self._call_discovery(None)
        self.assertIsInstance(result, dict)
        self.assertIn("available_filters", result)
        self.assertIsInstance(result["available_filters"], list)

    def test_list_gegl_filters_filtering(self):
        result = self._call_discovery("blur")
        self.assertIsInstance(result, dict)
        for f in result.get("available_filters", []):
            self.assertIn("name", f)
            self.assertIn("category", f)
            # In mock data, blur filters have category 'blur'
            self.assertTrue(f["category"] == "blur" or ("blur" in f["name"]))

    def test_list_gegl_filters_cache_mechanism(self):
        # First call to populate cache
        first = self._call_discovery(None)
        self.assertIsNotNone(first)
        # Immediate second call should return same object (cache hit)
        second = self._call_discovery(None)
        self.assertIs(first, second)

        # Expire cache and ensure a refreshed object is returned
        import time as _time
        import gimp_mcp_server as _gms

        if hasattr(_gms, "_cache_timestamp"):
            _gms._cache_timestamp = _gms._cache_timestamp - 301
        refreshed = self._call_discovery(None)
        self.assertIsNot(refreshed, first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
