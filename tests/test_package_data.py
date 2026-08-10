"""Non-Python files must both exist on disk and actually ship in the wheel.

This gap caused a real break. `config/file.py` read the shipped example via
`importlib.resources.files(__package__)`; when `settings.py` moved into the `config/`
subpackage, `__package__` became `"kokua.config"` while the file stayed at the package root. The
editable install resolved it anyway (the whole source tree is importable), so every test passed --
a built wheel would have raised at runtime.

So these tests deliberately do not trust the editable layout. They check the two halves of the
contract separately:

1. every resource the code reads at runtime is declared in `[tool.setuptools.package-data]`, so it
   is in the wheel at all; and
2. every anchor passed to `files(...)` is the package the resource actually sits under, so the
   lookup resolves once the source tree is no longer implicitly importable.

Static and instant -- no wheel build, no network -- so it stays in the default suite.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "kokua"


def _package_data() -> dict[str, list[str]]:
    """The declared `[tool.setuptools.package-data]` table: package -> glob patterns."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]


def _package_dir(package: str) -> Path:
    return SRC_ROOT / Path(*package.split("."))


def _declared_files() -> set[Path]:
    """Every file on disk matched by a package-data glob, as a repo-relative path."""
    matched: set[Path] = set()
    for package, patterns in _package_data().items():
        root = _package_dir(package)
        for pattern in patterns:
            matched.update(path.relative_to(REPO_ROOT) for path in root.glob(pattern) if path.is_file())
    return matched


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so a joinpath arg can be a named constant.

    `config/file.py` passes `EXAMPLE_FILENAME` rather than a literal -- and that is the call site
    this whole module exists because of, so resolving these is not optional.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def _resource_reads() -> list[tuple[Path, int, str, str]]:
    """Every `files(<anchor>).joinpath(<path>)` in the source, as (file, line, anchor, path).

    An f-string argument contributes its literal prefix (``f"web_static/{name}"`` ->
    ``web_static/``), which is the directory that must exist. A named module constant is resolved
    to its value. Only literal anchors are collected; a computed one is caught by
    ``test_no_resource_anchor_is_computed``.
    """
    found: list[tuple[Path, int, str, str]] = []
    for source_file in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "joinpath":
                continue
            anchor_call = node.func.value
            if not (isinstance(anchor_call, ast.Call) and _is_files_call(anchor_call)):
                continue
            if not (anchor_call.args and isinstance(anchor_call.args[0], ast.Constant)):
                continue  # computed anchor; covered by its own test
            anchor = anchor_call.args[0].value
            for arg in node.args:
                literal = _literal_prefix(arg, constants)
                if literal:
                    found.append((source_file.relative_to(REPO_ROOT), node.lineno, anchor, literal))
    return found


def _is_files_call(call: ast.Call) -> bool:
    """True for `files(...)` or `importlib.resources.files(...)`."""
    func = call.func
    return (isinstance(func, ast.Name) and func.id == "files") or (
        isinstance(func, ast.Attribute) and func.attr == "files"
    )


def _literal_prefix(node: ast.expr, constants: dict[str, str]) -> str:
    """The literal leading text of a string arg: a literal, a named constant, or an f-string prefix."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id, "")
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


# --- the declarations describe reality ----------------------------------------------------------


def test_every_package_data_glob_matches_a_real_file():
    """A glob matching nothing means a resource silently stopped shipping."""
    for package, patterns in _package_data().items():
        root = _package_dir(package)
        assert root.is_dir(), f"package-data declares {package!r}, which is not a package directory"
        for pattern in patterns:
            assert any(root.glob(pattern)), f"[package-data] {package}: {pattern!r} matches no file"


def test_package_data_packages_are_real_packages():
    for package in _package_data():
        assert (_package_dir(package) / "__init__.py").is_file(), f"{package!r} has no __init__.py"


# --- the code reads only what ships -------------------------------------------------------------


def test_source_reads_at_least_the_known_resources():
    """Guard the guard: if the AST scan silently stops finding call sites, these tests go quiet."""
    reads = _resource_reads()
    assert len(reads) >= 4, f"expected to find the known files(...).joinpath(...) sites, got {reads}"
    assert {anchor for _f, _l, anchor, _p in reads} == {"kokua"}


@pytest.mark.parametrize("source_file,line,anchor,resource", _resource_reads())
def test_resource_resolves_under_its_anchor(source_file, line, anchor, resource):
    """The anchor must be the package the resource actually lives under.

    This is the check that would have caught `files(__package__)` after the config/ move: it fails
    for an anchor that only resolves because the whole source tree happens to be importable.
    """
    target = _package_dir(anchor) / resource
    assert target.exists(), f"{source_file}:{line} reads {resource!r} under {anchor!r}, which does not exist there"


@pytest.mark.parametrize("source_file,line,anchor,resource", _resource_reads())
def test_resource_is_declared_as_package_data(source_file, line, anchor, resource):
    """A resource the code reads but package-data omits works in a source tree and fails in a wheel."""
    target = (_package_dir(anchor) / resource).relative_to(REPO_ROOT)
    declared = _declared_files()
    covered = target in declared or any(str(path).startswith(str(target)) for path in declared)
    assert covered, f"{source_file}:{line} reads {target}, which no [package-data] glob ships"


def test_no_resource_anchor_is_computed():
    """`files(__package__)` resolves to wherever the *reading module* lives, not where the file does.

    Moving that module into a subpackage then breaks the lookup in a wheel while the editable
    install keeps working. Anchor resources to a literal package name instead.
    """
    # AST, not a text search: a comment or docstring explaining this hazard is not an instance of it
    # (config/file.py carries exactly such a comment, and a regex flags it).
    offenders = []
    for source_file in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_files_call(node) and node.args:
                anchor = node.args[0]
                if isinstance(anchor, ast.Name) and anchor.id in ("__package__", "__name__"):
                    offenders.append(f"{source_file.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, "use a literal package name as the resource anchor: " + ", ".join(offenders)


# --- the shipped example is reachable the way the app reaches it ----------------------------------


def test_example_config_is_readable_through_the_resource_api():
    from kokua.config import example_text

    text = example_text()
    assert "[assistant]" in text and len(text) > 1000


def test_web_static_index_is_readable_through_the_resource_api():
    from importlib.resources import files

    html = files("kokua").joinpath("web_static/index.html").read_text(encoding="utf-8")
    assert "<html" in html.lower()


def test_every_web_static_asset_the_page_loads_is_shipped():
    """The page's own <script>/<link> tags are the real list of assets that must ship."""
    from importlib.resources import files

    html = files("kokua").joinpath("web_static/index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:src|href)="/?([\w.-]+\.(?:js|css))"', html))
    assert referenced, "found no local asset references in index.html; has the page changed shape?"

    patterns = _package_data()["kokua"]
    for asset in sorted(referenced):
        path = PACKAGE_ROOT / "web_static" / asset
        assert path.is_file(), f"index.html loads {asset!r}, which is not in web_static/"
        assert any(fnmatch.fnmatch(f"web_static/{asset}", pattern) for pattern in patterns), (
            f"index.html loads {asset!r}, which no [package-data] glob ships"
        )
