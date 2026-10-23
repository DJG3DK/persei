"""Splitting a project's memory, and choosing what of it stays resident.

The property everything else rests on is that a section is a SLICE of the
file: preamble plus every body, joined in index order, is the original, byte
for byte. If that ever stops being true, a nightly consolidation writes a
memory that loses a paragraph nobody can name, and no other test in this
suite would notice.

The rest of this file is the operator's policy (`stays_always` in agent/memory_sections.py)
asserted against a memory shaped like a real one -- what gets pinned, what
gets indexed, and whether an index entry says anything a title does not.
"""

from agent import memory_sections as ms
from tests.fixture_memory import EXAMPLE_MEMORY

SMALL_MEMORY = """# Project memory: demo

A small project that has not learned much yet.

## Testing

Run the suite with `.venv/bin/pytest`. A bare `pytest` collects nothing.

## Deploying

`scripts/deploy.sh`, and nothing else.
"""


# --- nothing is ever lost --------------------------------------------------

def test_splitting_then_reassembling_returns_the_original_byte_for_byte():
    preamble, sections = ms.split_sections(EXAMPLE_MEMORY)
    assert ms.render_full(preamble, sections) == EXAMPLE_MEMORY


def test_reassembly_survives_the_shapes_a_real_memory_has():
    """A heading with punctuation, two headings with the same text, a heading
    with trailing hashes, CRLF, and a `##` line inside a code fence."""
    text = (
        "Preamble, no heading above it.\r\n\r\n"
        "## Notes (2026-01-02): the `--fast` flag ##\r\n\r\n"
        "It does not do what it says.\r\n\r\n"
        "## Notes\r\n\r\n"
        "```markdown\r\n## Not a section\r\n```\r\n\r\n"
        "## Notes\r\n\r\nThe third one.\r\n"
    )
    preamble, sections = ms.split_sections(text)
    assert ms.render_full(preamble, sections) == text
    # three headings, not four: the one inside the fence is not a section
    assert [s.slug for s in sections] == ["notes-2026-01-02-the-fast-flag", "notes", "notes-2"]


def test_a_heading_inside_a_code_fence_does_not_start_a_section():
    """Memory files quote markdown at each other. Splitting inside a fence
    still reassembles, but it produces a section whose body is the back half
    of somebody's example."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    assert "what-changed" not in [s.slug for s in sections]
    body = next(s.body for s in sections if s.slug == "past-incidents-worth-remembering")
    assert "## What changed" in body


def test_a_file_with_no_headings_is_all_preamble():
    preamble, sections = ms.split_sections("just a paragraph, no headings at all\n")
    assert sections == []
    assert preamble == "just a paragraph, no headings at all\n"


# --- Rule 0: a small memory is not split at all ----------------------------

def test_a_memory_under_the_floor_is_not_split():
    """The index costs prompt tokens and a section costs a tool call. Below
    the floor the machinery costs more than the file it is saving, and a
    brand-new project's starter memory must not change shape at all."""
    assert len(SMALL_MEMORY) < ms.SPLIT_FLOOR_CHARS
    assert not ms.is_worth_splitting(SMALL_MEMORY)


def test_a_memory_over_the_floor_with_real_sections_is_split():
    assert len(EXAMPLE_MEMORY) >= ms.SPLIT_FLOOR_CHARS
    assert ms.is_worth_splitting(EXAMPLE_MEMORY)


def test_one_enormous_section_is_not_worth_splitting():
    """One section plus an index entry pointing at it is strictly worse than
    the file."""
    text = "Preamble.\n\n## Everything\n\n" + ("x" * 20_000)
    assert not ms.is_worth_splitting(text)


# --- Rule 1: what stays in the prompt --------------------------------------

def _always_slugs(text: str) -> set[str]:
    _, sections = ms.split_sections(text)
    return {e.slug for e in ms.build_index(sections) if e.always}


def test_the_always_block_holds_the_rules_that_fail_silently():
    """Sandbox constraints, test wiring, collision traps and configuration:
    the four families whose failure mode is that nothing tells you."""
    always = _always_slugs(EXAMPLE_MEMORY)
    assert {"working-in-the-sandbox", "testing", "name-collisions-to-watch-for",
            "environment-variables-and-secrets"} <= always


def test_the_big_sections_are_indexed_however_important_they_are():
    """The size clause is what keeps the rule honest. Conventions is important
    by any measure; pinning it would spend most of the saving on one section,
    and a convention is something the agent knows it is about to need the
    moment it starts writing code."""
    always = _always_slugs(EXAMPLE_MEMORY)
    assert "conventions" not in always
    assert "dependency-remediation" not in always
    assert "frontend-patterns" not in always


def test_the_same_title_lands_differently_by_what_is_in_it():
    """The rule reads the section, not the name -- which is the whole reason
    it is not a list of pinned headings. The fixture's own "Conventions" is
    2,664 chars and is indexed; the same title on a short section that says
    its violations ship unnoticed is pinned, which is how "Conventions" lands
    differently in two real projects."""
    small = ms.Section(
        title="Conventions", slug="conventions",
        body=("## Conventions\n\nMoney is integer cents, in `worker/money.py` and everywhere else. "
              "A float here fails silently: the total is out by a fraction and nothing raises.\n"),
    )
    assert ms.stays_always(small)
    assert "convention" in ms.always_reason(small)
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    assert not ms.stays_always(next(s for s in sections if s.slug == "conventions"))


def test_a_consolidation_leftover_is_not_a_rule():
    """The fixture's duplicate "Conventions" is 224 chars of housekeeping
    ("a second heading with the same title, from a later run"). It matches a
    family on its title and fits the size clause, so before the body had to
    corroborate it was pinned into every prompt of every call -- while the
    real 2,664-char Conventions was indexed. That inverts the selection: the
    shorter and emptier a section is, the likelier it was to win."""
    always = _always_slugs(EXAMPLE_MEMORY)
    assert "conventions-2" not in always


def test_a_title_word_alone_does_not_pin_the_weakest_two_families():
    """"convention", "style", "rule", "migration" in a heading is close to a
    free pin. For those families Rule 1's second clause -- does it fail
    silently -- has to be answered by the section, not assumed from a word."""
    for title, body in [
        ("Lint rules", "## Lint rules\n\nLine length is 120. Imports are sorted. See `pyproject.toml`.\n"),
        ("Data model and migrations",
         "## Data model and migrations\n\nMigrations are numbered SQL files under `worker/migrations/`, "
         "applied in order on startup.\n"),
    ]:
        assert not ms.stays_always(ms.Section(title=title, slug="x", body=body)), title


def test_a_gotcha_is_not_a_trap():
    """The operator's policy uses a build gotcha as its worked example of what
    must NOT be pinned -- it fails loudly, the agent reads the error and goes
    looking. The word was pinning "Lint gotchas", a real heading, forever."""
    section = ms.Section(title="Lint gotchas", slug="lint-gotchas",
                         body="## Lint gotchas\n\nRuff needs the venv. See `pyproject.toml` for the rules.\n")
    assert not ms.stays_always(section)


def test_the_section_that_says_which_source_is_authoritative_is_pinned():
    """A number from the wrong source arrives looking exactly like the right
    one, nothing objects, and it gets quoted to a human as fact. That is as
    silent as a failure gets, and no other family covers it."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    section = next(s for s in sections if s.slug == "billing-figures-come-from-the-ledger")
    assert ms.stays_always(section)
    assert "authority" in ms.always_reason(section)


def test_one_passing_mention_of_a_silent_failure_does_not_pin_a_section():
    """The supplier-API section mentions one silently-wrong mapping in two
    thousand characters of pagination mechanics. That is an aside, not a
    subject."""
    assert "working-with-the-supplier-apis" not in _always_slugs(EXAMPLE_MEMORY)


def test_a_section_that_opens_by_saying_it_fails_quietly_is_pinned():
    section = ms.Section(
        title="Cache invalidation",
        slug="cache-invalidation",
        body=("## Cache invalidation\n\nForgetting to bump the cache key fails silently: the page renders "
              "the previous build and no error is raised anywhere.\n"),
    )
    assert ms.stays_always(section)
    assert "quietly" in ms.always_reason(section)


def test_the_always_block_stops_growing_at_the_cap():
    """A memory with twenty small sections that all qualify must not put the
    whole file back in the prompt one qualifying section at a time."""
    body = "Run the tests. It fails silently otherwise.\n" + ("word " * 400)
    text = "Preamble.\n\n" + "".join(f"## Testing {n}\n\n{body}\n" for n in range(20))
    preamble, sections = ms.split_sections(text)
    chosen = ms.choose_always(sections)
    assert 0 < len(chosen) < len(sections)
    entries = ms.build_index(sections, preamble=preamble)
    bodies = {s.slug: s.body for s in sections}
    assert ms.estimate_tokens(ms.render_prompt_block(preamble, entries, bodies)) <= ms.INLINE_TOKEN_BUDGET


def test_the_whole_rendered_block_is_inside_the_budget():
    """The floor this subsystem exists to create, on a memory shaped like a
    real one. The index is charged on top of the pinned bodies and grows with
    every section the consolidator invents, so a cap on the bodies alone is
    not a floor -- it was 12,000 chars (~3,000 tok) of bodies against a stated
    2,500-token budget, and the dry run printed a number the runtime was not
    held to."""
    preamble, sections = ms.split_sections(EXAMPLE_MEMORY)
    entries = ms.build_index(sections, preamble=preamble)
    bodies = {s.slug: s.body for s in sections}
    block = ms.render_prompt_block(preamble, entries, bodies)
    assert ms.estimate_tokens(block) <= ms.INLINE_TOKEN_BUDGET
    assert [e.slug for e in entries if e.always]  # ...and it is not empty


def test_an_index_big_enough_to_spend_the_budget_pins_nothing():
    """The index is what makes every section reachable; there is no version of
    this where dropping it to keep one pinned body is the better trade."""
    body = "Run the tests. It fails silently otherwise.\n"
    text = "Preamble.\n\n" + "".join(f"## Testing {n}\n\n{body}\n" for n in range(200))
    preamble, sections = ms.split_sections(text)
    entries = ms.build_index(sections, preamble=preamble)
    assert not [e.slug for e in entries if e.always]


def test_pinned_sections_keep_the_file_s_own_order():
    """A memory reads as an argument. Reordering it by size mid-prompt makes
    it read as a list of unrelated assertions."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    chosen = ms.choose_always(sections)
    assert chosen == [s.slug for s in sections if s.slug in set(chosen)]


# --- the index is the whole bet --------------------------------------------

def test_an_index_entry_says_what_is_in_the_section_not_its_title_again():
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    entry = next(e for e in ms.build_index(sections) if e.slug == "dependency-remediation")
    assert entry.summary
    # something from the body, not from the heading
    assert "worker/requirements.txt" in entry.summary
    assert entry.summary.lower() != entry.title.lower()


def test_an_index_entry_names_the_files_that_should_send_you_to_it():
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    by_slug = {e.slug: e for e in ms.build_index(sections)}
    assert "data/ledger.jsonl" in by_slug["billing-figures-come-from-the-ledger"].summary
    assert "Read before touching" in by_slug["the-nightly-reconciler"].summary


def test_an_entry_is_built_from_a_whole_sentence_not_a_wrapped_line():
    """These files are hard-wrapped at about 78 columns. An entry built from
    the first LINE reads as though it was cut off, which is what a reader
    takes it for."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    for entry in ms.build_index(sections):
        first = entry.summary.split(" Read before ")[0]
        assert not first or first.rstrip().endswith((".", "!", "?", ":", "...")), entry.summary


def test_a_broad_section_does_not_advertise_itself_as_a_narrow_one():
    """The trigger is the whole reason an on-demand section gets read, and a
    closed list of three files is a promise that the section is about those
    three. The fixture's Conventions also governs naming, commits, logging,
    component layout and CSS -- a model writing code anywhere else reads
    "before touching worker/money.py, frontend/src/format.ts" and concludes
    it does not need it. That is the miss the policy accepts the risk of, and
    the entry was making it likelier rather than less."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    entry = next(e for e in ms.build_index(sections) if e.slug == "conventions")
    assert "other files across this repo" in entry.summary


def test_a_framework_name_is_not_offered_as_a_file_to_read():
    """An index entry that sends a model to a file that does not exist is
    worse than one that says nothing -- and with room for three names, an
    invented one pushes out the real path that would have worked."""
    assert ms.cited_paths("We use Next.js and Node.js; see `web/src/store.ts`.") == ["web/src/store.ts"]


def test_the_files_a_memory_actually_cites_are_all_found():
    """Lockfiles, Dockerfiles and stylesheets are what memory prose names, and
    all of them used to fall out of the extractor -- which is how a section
    ended up advertising two frameworks and nothing else."""
    found = ms.cited_paths("See requirements.txt, index.html, styles.css, Dockerfile, "
                           "Makefile, poetry.lock, main.rs and pyproject.toml.")
    assert found == ["requirements.txt", "index.html", "styles.css", "Dockerfile",
                     "Makefile", "poetry.lock", "main.rs", "pyproject.toml"]


def test_a_url_is_not_mistaken_for_a_file_in_this_repo():
    """A real entry told the agent to read
    "action/releases/download/codeql-bundle-linux64.tar.gz", carved out of the
    middle of a download URL."""
    text = "Get it from https://github.com/github/codeql-action/releases/download/v2/b.tar.gz, then run src/core/bot.js."
    assert ms.cited_paths(text) == ["src/core/bot.js"]


def test_an_entry_is_not_cut_off_at_an_abbreviation():
    """A summary that stops at "(e.g." reads as a bug in the memory rather
    than as a summary of it, on every call of every task."""
    section = ms.Section(title="Pairs", slug="pairs", body=(
        "## Pairs\n\nPair identity is symbol_direction (e.g. `btc_long`), and nothing else builds one.\n"))
    assert "and nothing else builds one" in ms.describe_section(section)


def test_a_trigger_prefers_the_paths_that_point_somewhere():
    """A path with a slash names one place in the tree; a bare filename may be
    any of several, so it is the weaker signal when both are on offer."""
    section = ms.Section(title="Money", slug="money", body=(
        "## Money\n\nSee format.ts and `worker/money.py` and `frontend/src/format.ts` "
        "and `docs/money.md`.\n"))
    assert "Read before touching worker/money.py, frontend/src/format.ts, docs/money.md" in \
        ms.describe_section(section)


def test_an_entry_for_a_list_section_describes_the_list_not_its_first_item():
    """Presenting bullet one as the section's subject says something untrue
    about the other nine."""
    section = ms.Section(title="Lint rules", slug="lint-rules", body=(
        "## Lint rules\n\n- Line length is 120.\n- No bare `except`.\n- Imports are sorted.\n"))
    summary = ms.describe_section(section)
    assert "Line length is 120" in summary and "Imports are sorted" in summary


def test_an_entry_does_not_describe_a_section_by_its_own_housekeeping():
    """"This section is long because the work is fiddly" is what a
    consolidator writes when it is explaining itself, and it turns up at the
    top of exactly the long sections whose entry has the most work to do."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    entry = next(e for e in ms.build_index(sections) if e.slug == "dependency-remediation")
    assert "This section is long" not in entry.summary
    assert "scanner" in entry.summary


def test_code_inside_a_fence_is_not_read_as_the_section_s_first_claim():
    section = ms.Section(title="Running the checks", slug="running-the-checks", body=(
        "## Running the checks\n\n```\nmake check\n```\n\nIt runs ruff and the full suite.\n"))
    assert ms.describe_section(section).startswith("It runs ruff and the full suite.")


def test_a_trigger_of_bare_keywords_says_it_has_no_trigger_instead():
    """"Read before working on except" names nothing a task can be matched
    against, and it crowds out whatever would have."""
    section = ms.Section(title="Supported versions", slug="supported-versions", body=(
        "## Supported versions\n\n| python | ok |\n|---|---|\n| 3.12 | yes |\n"))
    assert "No trigger recorded" in ms.describe_section(section)


def test_a_long_heading_makes_a_slug_a_model_can_retype():
    """A slug is something a model copies out of an index line it read a
    thousand tokens ago. Cut mid-word it reads as a typo, and a guessed slug
    is a section not read."""
    slug = ms.slugify("Test wiring rules (critical): get these wrong and tests silently pass")
    assert not slug.endswith("-")
    assert slug in "test-wiring-rules-critical-get-these-wrong-and-tests-silently-pass"
    assert slug.split("-")[-1] in {"tests", "and", "wrong", "these", "get"}


def test_a_pinned_entry_with_no_body_is_advertised_as_readable():
    """The worst line this module can emit: "already above, do not re-read"
    printed next to a gap, which removes the content AND withdraws permission
    to go and get it. `always` is a flag in one file and the body is a
    separate key, so nothing guarantees the two agree."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    entries = ms.build_index(sections)
    pinned = next(e for e in entries if e.always)
    bodies = {s.slug: s.body for s in sections if s.slug != pinned.slug}
    block = ms.render_prompt_block("Preamble.", entries, bodies)
    assert "already above" not in block.split(f"- {pinned.slug} ")[1].split("\n")[0]
    assert pinned.summary in block


def test_the_index_records_what_it_was_cut_from():
    """So the reader can tell a section set that is still faithful to
    /AGENTS.md from one that a later write has left behind."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    raw = ms.index_to_json(ms.build_index(sections),
                           source_sha256=ms.source_digest(EXAMPLE_MEMORY), migrated_at="2026-09-21")
    doc = ms.parse_index_document(raw)
    assert doc.source_sha256 == ms.source_digest(EXAMPLE_MEMORY)
    assert doc.migrated_at == "2026-09-21"
    assert [e.slug for e in doc.entries] == [s.slug for s in sections]


def test_an_index_with_no_recorded_source_makes_no_claim_about_one():
    doc = ms.parse_index_document(ms.index_to_json(ms.build_index([])))
    assert doc.source_sha256 == ""


def test_a_path_with_a_json_extension_is_not_truncated_to_js():
    assert ms.cited_paths("bump it in `package.json`") == ["package.json"]


def test_the_index_marks_what_is_already_in_the_prompt():
    """An entry the model cannot tell apart is an entry it may re-fetch, and
    paying a tool call for text it is looking at is the one way this design
    costs more than it saves."""
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    block = ms.render_index_block(ms.build_index(sections))
    assert "- testing -- Testing (already above, do not re-read)" in block
    assert "read_memory_section" in block


def test_every_section_appears_in_the_index_exactly_once():
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    slugs = [e.slug for e in ms.build_index(sections)]
    assert slugs == [s.slug for s in sections]
    assert len(set(slugs)) == len(slugs)


# --- the stored index ------------------------------------------------------

def test_the_index_round_trips_through_json():
    _, sections = ms.split_sections(EXAMPLE_MEMORY)
    entries = ms.build_index(sections)
    parsed = ms.parse_index(ms.index_to_json(entries, source_sha256="abc", migrated_at="2026-09-21"))
    assert [(e.slug, e.title, e.summary, e.always) for e in parsed] \
        == [(e.slug, e.title, e.summary, e.always) for e in entries]


def test_an_unreadable_index_degrades_to_no_sections_rather_than_raising():
    """Which is a whole-file read -- today's behaviour -- rather than a prompt
    that cannot be built."""
    assert ms.parse_index("{not json at all") == []
    assert ms.parse_index('{"sections": "a string"}') == []
    assert ms.parse_index('{"sections": [{"no_slug": 1}]}') == []


def test_a_hand_edited_entry_missing_fields_still_loads():
    entries = ms.parse_index('{"sections": [{"slug": "testing"}]}')
    assert entries[0].slug == "testing" and entries[0].title == "testing"
    assert entries[0].always is False
