import glob
import importlib
from os.path import dirname, basename, isfile

def _list_all_modules():
    folder = dirname(__file__)
    pattern = f"{folder}/*.py"
    return sorted(
        basename(f)[:-3]
        for f in glob.glob(pattern)
        if isfile(f)
        and not f.endswith("__init__.py")
    )

_ALL_ = _list_all_modules()

for module_name in _ALL_:
    module = importlib.import_module(f"{__name__}.{module_name}")
    globals()[module_name] = module

__all__ = _ALL_