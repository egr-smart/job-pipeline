from pathlib import Path

from job_pipeline.config import load_config, load_env_file

MINIMAL = "home_country: au\ntitles: {include: [platform]}\n"


def test_env_file_loads_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nA_KEY=one\nexport B_KEY="two words"\nC_KEY=\'x=y\'\n\nnot a pair\n')
    monkeypatch.setenv("A_KEY", "from-shell")
    for k in ("B_KEY", "C_KEY"):
        monkeypatch.delenv(k, raising=False)
    load_env_file(env)
    import os
    assert (os.environ["A_KEY"], os.environ["B_KEY"], os.environ["C_KEY"]) == ("from-shell", "two words", "x=y")


def test_load_config_reads_dotenv_next_to_config(tmp_path, monkeypatch):
    monkeypatch.delenv("JP_TEST_SECRET", raising=False)
    (tmp_path / "config.yaml").write_text(MINIMAL)
    (tmp_path / ".env").write_text("JP_TEST_SECRET=s3cret\n")
    cfg = load_config(tmp_path / "config.yaml")
    import os
    assert os.environ["JP_TEST_SECRET"] == "s3cret" and cfg.home_country == "AU"


def test_inputs_and_state_resolve_separately(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(MINIMAL)
    monkeypatch.delenv("JOBS_STATE_DIR", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.path("profile.md") == tmp_path.resolve() / "profile.md"
    assert cfg.db_path == tmp_path.resolve() / "jobs.db"  # local default: state alongside config

    monkeypatch.setenv("JOBS_STATE_DIR", str(tmp_path / "state"))
    assert cfg.db_path == (tmp_path / "state" / "jobs.db").resolve()
    assert cfg.state_path("reports") == (tmp_path / "state" / "reports").resolve()
    assert cfg.path("profile.md") == tmp_path.resolve() / "profile.md"  # inputs unaffected
