"""Load the real extension against small ComfyUI host substitutes for tests."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_backend(root):
    from aiohttp import web

    package = types.ModuleType("catalog_test_extension")
    package.__path__ = [str(ROOT)]
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_user_directory = lambda: str(root)
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=web.RouteTableDef()))
    torch = types.ModuleType("torch")
    # NumPy has the indexing and shape operations used by the image conversion.
    # This substitute does not claim to test PyTorch or ComfyUI execution itself.
    torch.from_numpy = lambda pixels: pixels
    graph = types.ModuleType("comfy_execution.graph")
    class ExecutionBlocker:
        def __init__(self, message):
            self.message = message
    graph.ExecutionBlocker = ExecutionBlocker
    substitutes = {"folder_paths": folder_paths, "server": server, "torch": torch,
                   "catalog_test_extension": package, "comfy_execution.graph": graph}
    with patch.dict(sys.modules, substitutes):
        spec = importlib.util.spec_from_file_location("catalog_test_extension.nodes", ROOT / "nodes.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, substitutes
