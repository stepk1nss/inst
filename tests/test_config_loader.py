from contentbot.config.loader import load_all

from .conftest import ROOT, copy_config_root, write_project


def test_repo_projects_load(registry):
    assert not registry.errors, registry.errors
    assert {"zazerkalye", "spb"} <= set(registry.projects)
    assert {"quiz_teaser", "place", "simple_post"} <= set(registry.templates)


def test_pdd_scenario_resolves_template_and_overrides(registry):
    sc = registry.project("zazerkalye").scenarios["pdd_ticket"]
    assert sc.template == "quiz_teaser"
    assert sc.secret_fields() == {"correct", "correct_index", "explanation", "rule_ref", "uncertain"}
    assert "rule_ref" in sc.step("solve").produces
    assert sc.platform_defaults["telegram"].reveal_secrets is True
    assert "rule_ref" in sc.platform_defaults["telegram"].template
    for p in ("instagram", "tiktok", "youtube"):
        assert sc.platform_defaults[p].reveal_secrets is False


def test_core_has_no_project_specific_code():
    """The core must not know about any concrete project."""
    banned = ("зазеркал", "zazerkal", "пдд", "pdd", "батракан", "педаль", "петербург", "spb")
    for path in (ROOT / "src" / "contentbot").rglob("*.py"):
        if path.name == "demo.py":  # synthetic dev material, not used by the pipeline
            continue
        text = path.read_text(encoding="utf-8").lower()
        for word in banned:
            assert word not in text, f"{path} mentions '{word}'"


def test_broken_project_does_not_block_others(tmp_path):
    root = copy_config_root(tmp_path)
    write_project(root, "broken", {"id": "broken", "name": "Broken"})  # no scenarios
    reg = load_all(root)
    assert "broken" in reg.errors
    assert "zazerkalye" in reg.projects


def test_voice_id_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_PEDAL", "voice-123")
    reg = load_all(ROOT)
    assert reg.project("zazerkalye").voice.get("pedal").provider_voice_id == "voice-123"
