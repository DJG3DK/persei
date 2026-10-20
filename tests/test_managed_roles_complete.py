"""Every agent-* alias the router config defines must be a managed role, or
the Models page silently hides it (2026-09-09: agent-coder-frontend and
agent-planning-chat-frontend were pinned in config.yaml and described in
ROLE_REQUIREMENTS but missing from MANAGED_ROLES, so the operator could not
change them). The rule: all roles on the Models page, all changeable."""

import yaml

from agent.model_config import MANAGED_ROLES, ROLE_REQUIREMENTS
from agent.tools.model_rates import ROUTER_CONFIG_PATH


def _config_agent_aliases() -> set[str]:
    cfg = yaml.safe_load(ROUTER_CONFIG_PATH.read_text())
    return {e["model_name"] for e in cfg.get("model_list", []) if str(e.get("model_name", "")).startswith("agent-")}


def test_every_agent_alias_in_config_is_a_managed_role():
    missing = _config_agent_aliases() - set(MANAGED_ROLES)
    assert not missing, f"aliases invisible on the Models page: {sorted(missing)}"


def test_every_role_with_requirements_is_managed():
    missing = set(ROLE_REQUIREMENTS) - set(MANAGED_ROLES)
    assert not missing, sorted(missing)


def test_frontend_seats_are_managed_with_readable_labels():
    assert MANAGED_ROLES["agent-coder-frontend"] == "Coder (Frontend)"
    assert MANAGED_ROLES["agent-planning-chat-frontend"] == "Planning Chat (Frontend)"


def test_readme_names_every_managed_role_alias():
    """The Models tab lists every agent-* pin. If the README's role list
    omits one, an operator reading the docs cannot find it to repin -- the
    same class of silence that hid agent-coder-frontend from the page
    itself (see the module docstring)."""
    from pathlib import Path

    readme = Path("README.md").read_text()
    missing = [alias for alias in MANAGED_ROLES if f"`{alias}`" not in readme]
    assert not missing, f"README does not mention managed role(s): {missing}"


# ---------------------------------------------------------------------------
# The live config is the operator's file; the example is the repo's
# ---------------------------------------------------------------------------

def test_the_example_config_exists_and_parses():
    """services/model-router/config.yaml is gitignored -- the Models page
    rewrites it on every repin, so tracking it made each model change a diff
    and an upgrade could overwrite pins somebody chose. A fresh clone has only
    the example, and install.sh copies it into place, so the example is what
    must always be there and always be valid."""
    from pathlib import Path

    example = Path("services/model-router/config.example.yaml")
    assert example.is_file(), "install.sh copies this file; without it a fresh install has no aliases"
    parsed = yaml.safe_load(example.read_text())
    assert parsed.get("model_list"), "the example must define the aliases the agent asks for"


def test_the_example_covers_every_managed_role():
    """A role added to MANAGED_ROLES and not to the example means a fresh
    install ships a Models page listing a seat its own router cannot serve."""
    from pathlib import Path

    parsed = yaml.safe_load(Path("services/model-router/config.example.yaml").read_text())
    aliases = {e["model_name"] for e in parsed["model_list"]}
    missing = sorted(set(MANAGED_ROLES) - aliases)
    assert not missing, f"the example router config has no entry for: {missing}"


_FALLBACK_TARGETS = {"deepseek-v4-pro", "claude-haiku-4.5", "gpt-4o-mini"}

# Aliases this product asks for that are not chat seats, so they are neither
# agent-* nor a fallback target. `embedder` is the only one: agent/embeddings.py
# resolves it for episode recall, and naming it agent-embedder would have put a
# model with no chat, tool or structured-output behaviour into the Models
# page's seat picker.
# Aliases the product calls itself that are not chat seats: episode recall's
# embeddings, and the web search behind planning and the coder's research.
_NON_SEAT_ALIASES = {"embedder", "web-search"}


def test_the_example_does_not_ship_another_product_s_aliases():
    """install.sh copies this file onto every fresh box. Other applications
    that happen to share a router on the maintainer's machine are not
    Persei, and a visitor grepping the example should not find them."""
    from pathlib import Path

    parsed = yaml.safe_load(Path("services/model-router/config.example.yaml").read_text())
    aliases = {e["model_name"] for e in parsed["model_list"]}
    leftovers = sorted(
        name for name in aliases
        if not str(name).startswith("agent-")
        and name not in _FALLBACK_TARGETS
        and name not in _NON_SEAT_ALIASES
    )
    assert not leftovers, f"example config ships aliases that are not this product: {leftovers}"


def test_no_api_key_is_written_into_the_example():
    """It is committed, so it has to be a shape rather than a secret."""
    from pathlib import Path

    text = Path("services/model-router/config.example.yaml").read_text()
    for line in text.splitlines():
        if "api_key:" in line:
            assert "os.environ/" in line, f"a literal key in a committed file: {line.strip()[:60]}"


def test_the_config_path_falls_back_to_the_example(tmp_path, monkeypatch):
    """A checkout that has never been installed still has to answer "what
    models exist" -- the rate table, the Models page and these tests all read
    it, and import-time failures there read as a broken clone."""
    from agent.tools import model_rates

    router = tmp_path / "services" / "model-router"
    router.mkdir(parents=True)
    (router / "config.example.yaml").write_text("model_list: []\n")
    monkeypatch.delenv("MODEL_ROUTER_CONFIG_PATH", raising=False)

    assert model_rates._router_config_path(router).name == "config.example.yaml"

    (router / "config.yaml").write_text("model_list: []\n")
    assert model_rates._router_config_path(router).name == "config.yaml", \
        "the live file wins once it exists"

    monkeypatch.setenv("MODEL_ROUTER_CONFIG_PATH", str(tmp_path / "elsewhere.yaml"))
    assert model_rates._router_config_path(router).name == "elsewhere.yaml", \
        "an explicit override still wins over both"
