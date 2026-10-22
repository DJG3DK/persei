"""MODEL_ROUTER_KEY lives in two .env files, the router's and the agent's,
and the Environment page shows one row per file. The save path once looked
rows up by key NAME, so the agent row shadowed the router row: saving the
"Router master key" wrote the agent's .env and the router never saw the
change, and the two sides silently disagreed (2026-09-29). Rows are now
addressed by id."""
import dataclasses

import pytest

import agent.env_config as ec


@pytest.fixture
def files(monkeypatch, tmp_path):
    monkeypatch.delenv("TEKTONIX_BUNDLE", raising=False)
    router = tmp_path / "router.env"
    agent = tmp_path / "agent.env"
    router.write_text("MODEL_ROUTER_KEY=router-old\n")
    agent.write_text("MODEL_ROUTER_KEY=agent-old\n")
    keys = tuple(dataclasses.replace(mk, path=router if mk.path == ec.ROUTER_ENV else agent)
                 for mk in ec.MANAGED_KEYS)
    monkeypatch.setattr(ec, "MANAGED_KEYS", keys)
    return router, agent


def test_every_row_has_its_own_id():
    ids = [mk.row_id for mk in ec.MANAGED_KEYS]
    assert len(ids) == len(set(ids)), "two rows share an id, so one of them can never be saved"
    names = [mk.key for mk in ec.MANAGED_KEYS]
    assert names.count("MODEL_ROUTER_KEY") == 2, "the two router-key rows are the reason ids exist"


def test_the_page_names_the_row_beside_the_key(files):
    rows = {r["id"]: r for r in ec.list_keys()}
    assert rows["MODEL_ROUTER_KEY"]["key"] == "MODEL_ROUTER_KEY"
    assert rows["MODEL_ROUTER_KEY.agent"]["key"] == "MODEL_ROUTER_KEY"
    assert rows["MODEL_ROUTER_KEY"]["file"] == str(files[0])
    assert rows["MODEL_ROUTER_KEY.agent"]["file"] == str(files[1])
    assert rows["OPENROUTER_API_KEY"]["id"] == "OPENROUTER_API_KEY", "ordinary rows keep the key as their id"


def test_saving_the_master_key_writes_the_router_env(files):
    router, agent = files
    out = ec.set_keys({"MODEL_ROUTER_KEY": "router-new-value"})
    assert out["updated"] == ["MODEL_ROUTER_KEY"]
    assert "model-router" in out["restart_required"]
    assert "MODEL_ROUTER_KEY=router-new-value" in router.read_text()
    assert "MODEL_ROUTER_KEY=agent-old" in agent.read_text(), "the agent's file was written instead of the router's"


def test_saving_the_agent_side_key_writes_the_agent_env_under_the_variable_name(files):
    router, agent = files
    out = ec.set_keys({"MODEL_ROUTER_KEY.agent": "agent-new-value"})
    assert out["updated"] == ["MODEL_ROUTER_KEY.agent"]
    assert out["restart_required"] == ["tektonix"]
    assert "MODEL_ROUTER_KEY=agent-new-value" in agent.read_text()
    assert "MODEL_ROUTER_KEY.agent" not in agent.read_text(), "the row id leaked into the .env as a variable name"
    assert "MODEL_ROUTER_KEY=router-old" in router.read_text()


def test_both_rows_in_one_save_land_in_their_own_files(files):
    router, agent = files
    ec.set_keys({"MODEL_ROUTER_KEY": "same", "MODEL_ROUTER_KEY.agent": "same"})
    assert "MODEL_ROUTER_KEY=same" in router.read_text()
    assert "MODEL_ROUTER_KEY=same" in agent.read_text()


def test_a_value_with_a_newline_is_a_400_that_names_only_the_key(monkeypatch, files):
    """InvalidValueError fell through to the catch-all and came back as a
    500 "could not write the env file", which reads as a broken host rather
    than a bad value (2026-09-29)."""
    import agent.server as srv
    from agent.auth import User
    from fastapi.testclient import TestClient

    def admin():
        return User(id=1, email="a@b.co", role="admin", allowed_repos=None,
                    totp_enabled=True, must_change_password=False)

    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.get_current_user, admin)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, admin)
    router, agent = files
    r = TestClient(srv.app).post("/api/env-config", json={"updates": {"SMTP_HOST": "smtp.example\nAUTH_SECRET_KEY=x"}})
    assert r.status_code == 400, r.text
    assert "SMTP_HOST" in r.json()["detail"] and "smtp.example" not in r.json()["detail"]
    assert agent.read_text() == "MODEL_ROUTER_KEY=agent-old\n", "a refused value was written"


def test_a_duplicate_id_in_the_table_is_refused_rather_than_shadowed(monkeypatch, files):
    dup = tuple(dataclasses.replace(mk, id="") for mk in ec.MANAGED_KEYS)   # every id collapses to its key
    monkeypatch.setattr(ec, "MANAGED_KEYS", dup)
    with pytest.raises(RuntimeError, match="one id"):
        ec.set_keys({"OPENROUTER_API_KEY": "x"})


@pytest.mark.parametrize("sep", ["\n", "\r", "\x00", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
                                 "\u2028", "\u2029", "\x1b"])
def test_every_line_break_is_refused_before_anything_is_written(files, sep):
    """B28 (2026-10-22): the guard refused \\n, \\r and NUL; the others
    survived it, and the router's pm2 loader (line by line) read
    MODEL_ROUTER_ALLOW_UNAUTHENTICATED=1 from the next "line"."""
    router, agent = files
    with pytest.raises(ec.InvalidValueError):
        ec.set_keys({"MODEL_ROUTER_KEY": f"k{sep}MODEL_ROUTER_ALLOW_UNAUTHENTICATED=1"})
    with pytest.raises(ec.InvalidValueError):
        ec._format_value(f"k{sep}x")
    assert router.read_text() == "MODEL_ROUTER_KEY=router-old\n"


def test_a_tab_is_still_a_value_and_a_file_is_split_on_newlines_only(files):
    router, _ = files
    router.write_text("# keep\u2028this comment whole\nMODEL_ROUTER_KEY=old\n")
    ec.set_keys({"MODEL_ROUTER_KEY": "a\tb"})
    assert router.read_text() == '# keep\u2028this comment whole\nMODEL_ROUTER_KEY="a\tb"\n'
