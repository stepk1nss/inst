from contentbot.core.compose import merge_hashtags, visible_length
from contentbot.llm.schema_builder import build_output_schema


def _all_objects_closed(schema):
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for sub in schema["properties"].values():
            _all_objects_closed(sub)
    if schema.get("type") == "array":
        _all_objects_closed(schema["items"])


def test_teaser_schema_has_platform_texts_for_public_platforms(registry):
    project = registry.project("zazerkalye")
    sc = project.scenarios["pdd_ticket"]
    schema = build_output_schema(sc.step("teaser").produces, project, sc)
    _all_objects_closed(schema)
    platforms = schema["properties"]["platform_texts"]["properties"]
    assert set(platforms) == {"instagram", "tiktok", "youtube"}  # telegram uses the template
    assert "title" in platforms["youtube"]["properties"]


def test_hashtags_merge():
    assert merge_hashtags(["#пдд"], ["пдд", "#авто школа", "#водитель"], limit=5) == ["#пдд", "#автошкола", "#водитель"]
    assert merge_hashtags([], ["#a", "#b", "#c"], limit=2) == ["#a", "#b"]


def test_visible_length_ignores_html():
    assert visible_length("<tg-spoiler>abc</tg-spoiler> &amp;", True) == 5
