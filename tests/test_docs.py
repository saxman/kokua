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

A fourth check holds the "How agents work" catalogue to its own template. Thirteen pages, written by
different hands across three passes, are a catalogue rather than thirteen unrelated essays only if a
reader can predict a page's shape before opening it: where the transcript is, where the cost section
is, where to read next. That promise is cheap to keep while a page is being drafted and easy to lose
without anyone noticing, because a page missing or reordering a section still reads fine standing on
its own; only a sweep across every page catches the drift, which is why
`test_catalogue_pages_follow_the_template` checks it mechanically instead of trusting each new author
to have matched the last page they read.

A fifth check holds that catalogue's index to the directory it indexes, in both directions. The index
is the one file every one of the thirteen pages has to touch, so it is the file most likely to go
stale, and an unlisted page is invisible: `mkdocs.yml`'s nav would still surface it in the sidebar, so
nothing else notices, while the section's own table of contents quietly stops being a table of
contents. The reverse, an index entry naming a page that was renamed away, is caught by
`mkdocs build --strict` as well, but only where mkdocs is installed, and this check costs nothing.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"

# Brainstorming and planning artifacts are gitignored and unpublished, so they are not held to this.
PUBLISHED_PAGES = sorted(p for p in DOCS_DIR.rglob("*.md") if "superpowers" not in p.parts)

CATALOGUE_DIR = DOCS_DIR / "how-agents-work"
TEMPLATE_HEADINGS = ["## The idea", "## Watch it", "## In Kokua", "## What it costs", "## Go deeper"]
# get-it-running.md is setup, not a mechanism, and index.md is the section's table of contents; neither
# opens on a claim about how an agent works, so neither owes the reader the mechanism-page shape. A
# future page that is also not about a mechanism belongs in this set for the same reason.
NON_MECHANISM_PAGES = {"index.md", "get-it-running.md"}
MECHANISM_PAGES = sorted(p for p in CATALOGUE_DIR.glob("*.md") if p.name not in NON_MECHANISM_PAGES)

# Root files whose links are held to the same standard as a page's, for two different reasons.
# CHANGELOG.md and CONTRIBUTING.md are included verbatim into published pages, so their repository and
# site links reach the site even though the files themselves live outside docs_dir. README.md is not
# published, but it is the repository's front door and links *into* the site, so renaming a page
# 404s a README link with nothing else to catch it.
ROOT_PAGES = [REPO_ROOT / "CHANGELOG.md", REPO_ROOT / "CONTRIBUTING.md", REPO_ROOT / "README.md"]

CATALOGUE_INDEX = CATALOGUE_DIR / "index.md"
# A link to a sibling page of the catalogue, as the index writes them: bare filename, no directory.
CATALOGUE_LINK = re.compile(r"\]\(([a-z0-9-]+\.md)\)")

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


def _template_heading_violation(headings: list[str]) -> str | None:
    """Say what is wrong between a page's `##` headings and the template, or None if it matches.

    The three ways a page can drift are distinct enough to name separately: a section left out, a
    section that doesn't belong, or the five sections present but reordered. Naming which one gives
    the six-months-later author something to fix rather than a raw list to diff by hand.
    """
    if headings == TEMPLATE_HEADINGS:
        return None
    seen = set()
    for heading in headings:
        if heading in TEMPLATE_HEADINGS and heading in seen:
            return f"{heading!r} appears more than once"
        seen.add(heading)
    missing = [heading for heading in TEMPLATE_HEADINGS if heading not in headings]
    if missing:
        return f"missing {missing[0]!r}"
    extra = [heading for heading in headings if heading not in TEMPLATE_HEADINGS]
    if extra:
        return f"has {extra[0]!r}, which isn't part of the template"
    return f"headings out of order: found {headings}, expected {TEMPLATE_HEADINGS}"


def test_catalogue_pages_follow_the_template():
    """Every mechanism page must open on its claim and carry the same five sections in order.

    A catalogue page is trustworthy skimming material only if its shape doesn't vary: a reader who
    has learned that "What it costs" comes before "Go deeper" on one page should not have to re-learn
    it on the next. The one-sentence claim in bold, directly under the title, is part of that same
    promise: it's the page's answer before the reader commits to the section that argues for it.
    """
    violations = []
    for page in MECHANISM_PAGES:
        text = page.read_text()
        rel = page.relative_to(REPO_ROOT)

        headings = [line.strip() for line in text.splitlines() if line.startswith("## ")]
        problem = _template_heading_violation(headings)
        if problem:
            violations.append(f"{rel}: {problem}")

        paragraphs = text.split("\n\n", 2)
        claim = paragraphs[1] if len(paragraphs) > 2 else ""
        if not (claim.startswith("**") and claim.rstrip().endswith("**")):
            violations.append(f"{rel}: missing the bold one-sentence claim above '## The idea'")

    assert not violations, "catalogue pages diverging from the mechanism-page template:\n" + "\n".join(violations)


def _indexed_catalogue_pages() -> set[str]:
    """The pages the catalogue index lists, read from its "## Pages" section alone.

    Scoping to that one section is what makes the check mean what it says: the index also links a
    page or two from its prose (the note on conventions names one as an example), and a mention there
    is not the listing a reader navigates by.
    """
    text = CATALOGUE_INDEX.read_text()
    after_heading = text.split("## Pages", 1)[-1]
    return set(CATALOGUE_LINK.findall(after_heading.split("\n## ", 1)[0]))


def test_every_catalogue_page_is_listed_in_the_index():
    """The catalogue index has to name every page in its directory, and no page that is gone."""
    listed = _indexed_catalogue_pages()
    on_disk = {page.name for page in CATALOGUE_DIR.glob("*.md")} - {"index.md"}

    problems = [f"{name} exists but is not listed in the index" for name in sorted(on_disk - listed)]
    problems += [f"{name} is listed in the index but does not exist" for name in sorted(listed - on_disk)]
    assert not problems, "docs/how-agents-work/index.md and the pages beside it disagree:\n" + "\n".join(problems)
