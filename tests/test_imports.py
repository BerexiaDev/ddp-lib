import importlib
from importlib.metadata import distribution
import os
from pathlib import Path
import pkgutil
import unittest


class ImportSmokeTests(unittest.TestCase):
    def test_every_shipped_module_imports(self):
        package = importlib.import_module("ddp_lib")
        modules = list(pkgutil.walk_packages(package.__path__, package.__name__ + "."))
        self.assertTrue(modules)
        for module in modules:
            with self.subTest(module=module.name):
                importlib.import_module(module.name)
        if os.environ.get("DDP_INSTALL_SMOKE") == "1":
            installed = distribution("ddp-lib")
            self.assertEqual(installed.version, "0.2.0")
            self.assertEqual(Path(package.__file__).resolve(),
                             Path(installed.locate_file("ddp_lib/__init__.py")).resolve())
            self.assertNotIn(Path(__file__).resolve().parents[1], Path(package.__file__).parents)

    def test_document_driver_apis_still_exist(self):
        from pymongo.collection import Collection
        from bson import BSON
        for name in ("save", "remove", "insert"):
            self.assertTrue(callable(getattr(Collection, name, None)), name)
        value = {"_id": "user-123", "active": True}
        self.assertEqual(BSON(BSON.encode(value)).decode(), value)


if __name__ == "__main__":
    unittest.main()
