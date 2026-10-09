"""Build the JSON Schema for a generate step from the scenario's field specs."""

from __future__ import annotations

from typing import Any

from ..config.schema import FieldSpec, Project, Scenario

_SCALAR = {"string", "boolean", "integer", "number"}


def platform_texts_targets(project: Project, scenario: Scenario, field_name: str) -> list[str]:
    """Platforms whose text comes from `<field_name>.<platform>`."""
    out = []
    for pname in project.enabled_platforms():
        pd = scenario.platform_defaults.get(pname)
        if pd and pd.text_from == f"{field_name}.{pname}":
            out.append(pname)
    return out


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def field_schema(name: str, spec: FieldSpec, project: Project, scenario: Scenario) -> dict[str, Any]:
    if spec.type in _SCALAR:
        s: dict[str, Any] = {"type": spec.type}
    elif spec.type == "array":
        s = {"type": "array", "items": {"type": spec.items or "string"}}
    elif spec.type == "platform_texts":
        props: dict[str, Any] = {}
        for pname in platform_texts_targets(project, scenario, name):
            p_props: dict[str, Any] = {}
            if pname == "youtube":
                p_props["title"] = {"type": "string", "description": "Заголовок видео"}
            p_props["text"] = {"type": "string", "description": "Текст публикации без хэштегов"}
            p_props["hashtags"] = {"type": "array", "items": {"type": "string"}, "description": "Хэштеги с символом #"}
            props[pname] = _obj(p_props)
        s = _obj(props)
    else:  # pragma: no cover - guarded by the schema Literal
        raise ValueError(spec.type)
    if spec.desc:
        s["description"] = spec.desc
    return s


def build_output_schema(produces: list[str], project: Project, scenario: Scenario) -> dict[str, Any]:
    return _obj({name: field_schema(name, scenario.fields[name], project, scenario) for name in produces})
