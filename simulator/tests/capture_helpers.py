"""Load pure capture helpers without importing Docker-only client dependencies."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType


def load_helper(name):
    directory = Path(__file__).resolve().parents[2] / "im_client/communication"
    package = "_capture_test_helpers"
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(directory)]
        sys.modules[package] = module
    spec = importlib.util.spec_from_file_location(package + "." + name, directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
