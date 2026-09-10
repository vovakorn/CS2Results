import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
PRODUCTION_BOUNDARIES = (
    PROJECT_ROOT / "cs2bot" / "main.py",
    PROJECT_ROOT / "cs2bot" / "match_sources" / "match_fetcher.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_production_selector_and_handlers_do_not_import_legacy_sources():
    legacy_imports = {
        imported
        for path in PRODUCTION_BOUNDARIES
        for imported in _imports(path)
        if imported == "legacy"
        or imported.startswith("legacy.")
        or imported.endswith(".legacy")
        or ".legacy." in imported
    }

    assert not legacy_imports
