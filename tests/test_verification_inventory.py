"""Published contracts must point to real tests, not obsolete fixture names."""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_feature_inventory_references_existing_tests():
    manifest = json.loads((ROOT / "verification.json").read_text())
    inventories = sorted(ROOT.glob(manifest["inventory"]))
    assert inventories
    ids = set()
    for path in inventories:
        item = json.loads(path.read_text())
        assert item["id"] not in ids
        ids.add(item["id"])
        assert item["observable_contract"] and item["owner"]
        assert item["test_files"]
        for name in item["test_files"]:
            test = ROOT / name
            assert test.is_file(), (path, name)
            module = ast.parse(test.read_text())
            assert any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                for node in module.body
            ), (path, name)
