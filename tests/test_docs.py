"""Link guards for the published documentation site.

The site's claim is that Kokua's machinery can be followed, so a link into the source that does not
resolve is a defect in the thing this project exists to do. Three ways `docs/` can break that promise
are cheap to test for, and all three are the kind of rot a rename causes silently:

* A link into the repository that names a path which no longer exists.
* A relative link that escapes `docs/`. Those resolve in GitHub's file viewer and nowhere else:
  mkdocs cannot follow a target outside its `docs_dir`, so the published page 404s.
* A link to the published site itself (`https://saxman.info/kokua/<slug>/`) that names a page which
  no longer exists.

`mkdocs build --strict` in CI covers links *within* the site, resolved against the nav mkdocs already
built, but a `https://` URL is external to mkdocs by construction, so it validates nothing about a link
back to the very site it is building: renaming or removing a page would 404 every `saxman.info/kokua`
link pointing at it with `mkdocs build --strict` still green. That is a different failure from the
repository-link check above (a GitHub URL is checked against the repository tree, not against a doc
page), so it needs its own check even though both are, at the source level, "does this link still
resolve." These three cover the rest, and they need no mkdocs installed, so they run in the default
suite.

The checks below use different file sets on purpose. `CHANGELOG.md` and `CONTRIBUTING.md` at the
repository root are not part of `docs_dir`, but both are included verbatim into published pages, so a
repository link or a site link inside either one reaches the site too and is held to the same
resolves-or-fails standard. Neither file is held to the escaping-link check: both legitimately use
plain repository-relative links today (`docs/how-to/...`, not `../../docs/how-to/...`).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"

# Brainstorming and planning artifacts are gitignored and unpublished, so they are not held to this.
PUBLISHED_PAGES = sorted(p for p in DOCS_DIR.rglob("*.md") if "superpowers" not in p.parts)

# Root files included verbatim into published pages: their repository and site links reach the site
# too, even though the files themselves live outside docs_dir.
ROOT_PAGES = [REPO_ROOT / "CHANGELOG.md", REPO_ROOT / "CONTRIBUTING.md"]

REPO_LINK = re.compile(r"https://github\.com/saxman/kokua/(?:blob|tree)/main/([^)\s#]+)")
ESCAPING_LINK = re.compile(r"\]\(\.\./\.\./[^)]+\)")
SITE_LINK = re.compile(r"https://saxman\.info/kokua/([^)\s#]*)")


def test_source_links_resolve():
    missing = []
    for page in PUBLISHED_PAGES + ROOT_PAGES:
        for target in REPO_LINK.findall(page.read_text()):
            if not (REPO_ROOT / target).exists():
                missing.append(f"{page.relative_to(REPO_ROOT)} -> {target}")
    assert not missing, "documentation links naming paths that do not exist:\n" + "\n".join(missing)


def test_no_links_escape_the_docs_directory():
    escaping = []
    for page in PUBLISHED_PAGES:
        for link in ESCAPING_LINK.findall(page.read_text()):
            escaping.append(f"{page.relative_to(REPO_ROOT)}: {link}")
    assert not escaping, (
        "relative links escaping docs/ cannot resolve on the published site; use "
        "https://github.com/saxman/kokua/blob/main/<path> instead:\n" + "\n".join(escaping)
    )


def _site_slug_resolves(slug: str) -> bool:
    """Map a saxman.info/kokua/<slug>/ URL back to the page mkdocs builds it from.

    mkdocs serves the home page at the bare site root and every other page at a trailing-slash
    directory URL, backed by either `docs/<slug>.md` or `docs/<slug>/index.md`. A slug matching
    neither shape (missing the trailing slash a real site URL always has) cannot be a page mkdocs
    would ever emit, so it resolves to nothing rather than being given the benefit of the doubt.
    """
    if slug == "":
        return (DOCS_DIR / "index.md").exists()
    if not slug.endswith("/"):
        return False
    stem = slug.rstrip("/")
    return (DOCS_DIR / f"{stem}.md").exists() or (DOCS_DIR / stem / "index.md").exists()


def test_site_links_resolve():
    broken = []
    for page in PUBLISHED_PAGES + ROOT_PAGES:
        for slug in SITE_LINK.findall(page.read_text()):
            if not _site_slug_resolves(slug):
                broken.append(f"{page.relative_to(REPO_ROOT)} -> https://saxman.info/kokua/{slug}")
    assert not broken, "links to the published site naming a page that does not exist:\n" + "\n".join(broken)
