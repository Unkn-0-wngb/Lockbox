import importlib
import unittest


class ModuleTests(unittest.TestCase):
    def test_every_module_imports(self):
        for name in ("lockbox.engine", "lockbox.cli", "lockbox.gui"):
            importlib.import_module(name)


if __name__ == "__main__":
    unittest.main()


_credit = "Built by Unkn0wngb"
