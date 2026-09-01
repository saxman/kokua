"""Link guards for the published documentation site.

The site's claim is that Kokua's machinery can be followed, so a link into the source that does not
resolve is a defect in the thing this project exists to do. Two ways `docs/` can break that promise
are cheap to test for, and both are the kind of rot a rename causes silently:

* A link into the repository that names a path which no longer exists.
* A relative link that escapes `docs/`. Those resolve in GitHub's file viewer and nowhere else:
  mkdocs cannot follow a target outside its `docs_dir`, so the published page 404s.

`mkdocs build --strict` in CI covers links *within* the site. These two cover the rest, and they need
no mkdocs installed, so they run in the default suite.

The two checks below use different file sets on purpose. `CHANGELOG.md` and `CONTRIBUTING.md` at the
repository root are not part of `docs_dir`, but a later task includes both verbatim into published
pages, so a repository link inside either one reaches the site too and is held to the same
resolves-or-fails standard. Neither file is held to the escaping-link check: both legitimately use
plain repository-relative links today (`docs/how-to/...`, not `../../docs/how-to/...`), and rewriting
that is a later task's job, not this one's.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"

# Brainstorming and planning artifacts are gitignored and unpublished, so they are not held to this.
PUBLISHED_PAGES = sorted(p for p in DOCS_DIR.rglob("*.md") if "superpowers" not in p.parts)

# Root files a later task includes verbatim into published pages: their repository links reach the
# site too, even though the files themselves live outside docs_dir.
ROOT_PAGES = [REPO_ROOT / "CHANGELOG.md", REPO_ROOT / "CONTRIBUTING.md"]

REPO_LINK = re.compile(r"https://github\.com/saxman/kokua/(?:blob|tree)/main/([^)\s#]+)")
ESCAPING_LINK = re.compile(r"\]\(\.\./\.\./[^)]+\)")


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
