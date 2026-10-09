"""Load and resolve configuration: system.yaml, templates/*.yaml, projects/*/project.yaml."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .checks import check_project
from .schema import (
    CheckSpec,
    FieldSpec,
    PlatformDefault,
    Project,
    ProjectConfig,
    Scenario,
    ScenarioConfig,
    ScenarioTemplate,
    StepSpec,
    SystemConfig,
)

PLACEHOLDER_MARK = "<!-- PLACEHOLDER"
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Configuration is invalid. Message is human-readable (shown in the bot)."""


def _interpolate_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML error: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return _interpolate_env(data)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _fmt_validation(path: Path, e: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


@dataclass
class ConfigRegistry:
    root: Path
    system: SystemConfig
    templates: dict[str, ScenarioTemplate]
    projects: dict[str, Project] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # project dir -> error

    def project(self, project_id: str) -> Project:
        try:
            return self.projects[project_id]
        except KeyError:
            raise ConfigError(f"unknown project '{project_id}'") from None


def load_system(root: Path) -> SystemConfig:
    path = root / "config" / "system.yaml"
    try:
        return SystemConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(_fmt_validation(path, e)) from e


def load_templates(root: Path) -> dict[str, ScenarioTemplate]:
    templates: dict[str, ScenarioTemplate] = {}
    for path in sorted((root / "templates").glob("*.yaml")):
        try:
            tpl = ScenarioTemplate.model_validate(_read_yaml(path))
        except ValidationError as e:
            raise ConfigError(_fmt_validation(path, e)) from e
        if tpl.id in templates:
            raise ConfigError(f"{path}: duplicate template id '{tpl.id}'")
        templates[tpl.id] = tpl
    return templates


def _read_text_file(base: Path, rel: str, warnings: list[str]) -> str:
    path = (base / rel).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ConfigError(f"{rel}: path escapes the project directory")
    if not path.exists():
        raise ConfigError(f"{base / rel}: file not found")
    text = path.read_text(encoding="utf-8")
    if PLACEHOLDER_MARK in text:
        warnings.append(f"{rel}: материалы-заглушка, ещё не утверждены")
    return text


def resolve_scenario(
    sc: ScenarioConfig, templates: dict[str, ScenarioTemplate], project_dir: Path, warnings: list[str]
) -> Scenario:
    if sc.extends:
        if sc.extends not in templates:
            raise ConfigError(f"scenario '{sc.id}': unknown template '{sc.extends}'")
        tpl = templates[sc.extends]
        fields = {k: v.model_dump() for k, v in tpl.fields.items()}
        steps = {s.id: s.model_dump() for s in tpl.steps}
        step_order = [s.id for s in tpl.steps]
        checks = [c.model_dump() for c in tpl.checks]
        platforms = {k: v.model_dump() for k, v in tpl.platform_defaults.items()}
    else:
        fields = {k: v.model_dump() for k, v in sc.fields.items()}
        steps = {s.id: s.model_dump() for s in sc.steps}
        step_order = [s.id for s in sc.steps]
        checks = [c.model_dump() for c in sc.checks]
        platforms = {k: v.model_dump() for k, v in sc.platform_defaults.items()}

    ov = sc.overrides
    for name, patch in ov.fields.items():
        fields[name] = _deep_merge(fields.get(name, {}), patch)
    for step_id, patch in ov.steps.items():
        if step_id not in steps:
            raise ConfigError(f"scenario '{sc.id}': override for unknown step '{step_id}'")
        steps[step_id] = _deep_merge(steps[step_id], patch)
    for pname, patch in ov.platforms.items():
        merged = _deep_merge(platforms.get(pname, {}), patch)
        # a template override replaces text_from and vice versa
        if "template" in patch:
            merged.pop("text_from", None)
        if "text_from" in patch:
            merged.pop("template", None)
        platforms[pname] = merged
    checks += [c.model_dump() for c in sc.checks_add]

    knowledge = "\n\n".join(_read_text_file(project_dir, f, warnings) for f in sc.knowledge)
    try:
        return Scenario(
            id=sc.id,
            title=sc.title,
            tag=sc.tag,
            when=sc.when,
            template=sc.extends,
            instructions=sc.instructions,
            knowledge_text=knowledge,
            fields={k: FieldSpec.model_validate(v) for k, v in fields.items()},
            steps=[StepSpec.model_validate(steps[i]) for i in step_order],
            checks=[CheckSpec.model_validate(c) for c in checks],
            platform_defaults={k: PlatformDefault.model_validate(v) for k, v in platforms.items()},
        )
    except ValidationError as e:
        raise ConfigError(_fmt_validation(project_dir / f"scenario:{sc.id}", e)) from e


def load_project(project_dir: Path, templates: dict[str, ScenarioTemplate], step_types: set[str]) -> Project:
    path = project_dir / "project.yaml"
    try:
        cfg = ProjectConfig.model_validate(_read_yaml(path))
    except ValidationError as e:
        raise ConfigError(_fmt_validation(path, e)) from e

    warnings: list[str] = []
    brand = cfg.brand.text
    if cfg.brand.file:
        brand = (brand + "\n\n" + _read_text_file(project_dir, cfg.brand.file, warnings)).strip()
    knowledge = "\n\n".join(_read_text_file(project_dir, f, warnings) for f in cfg.knowledge.files)
    examples: list[str] = []
    if cfg.brand.examples_dir:
        ex_dir = project_dir / cfg.brand.examples_dir
        for p in sorted(ex_dir.glob("*")):
            if p.suffix in (".md", ".txt") and p.name.lower() != "readme.md":
                examples.append(p.read_text(encoding="utf-8"))

    scenarios: dict[str, Scenario] = {}
    for sc in cfg.scenarios:
        if sc.id in scenarios:
            raise ConfigError(f"{path}: duplicate scenario id '{sc.id}'")
        scenarios[sc.id] = resolve_scenario(sc, templates, project_dir, warnings)

    for v in cfg.voice.allowed:
        if cfg.voice.enabled and not v.provider_voice_id:
            warnings.append(f"голос '{v.id}': ID не задан, используется временный голос")

    project = Project(
        id=cfg.id,
        name=cfg.name,
        emoji=cfg.emoji,
        tag=cfg.tag,
        language=cfg.language,
        commercial=cfg.commercial,
        brand_text=brand,
        knowledge_text=knowledge,
        examples=examples,
        voice=cfg.voice,
        platforms=cfg.platforms,
        publishing=cfg.publishing,
        scenario_selection=cfg.scenario_selection,
        scenarios=scenarios,
        warnings=warnings,
    )
    problems = check_project(project, step_types)
    if problems:
        raise ConfigError(f"{path}: configuration errors:\n" + "\n".join(f"  - {p}" for p in problems))
    return project


def load_all(root: Path, step_types: set[str] | None = None) -> ConfigRegistry:
    """Load everything. A broken project does not stop the others from loading."""
    if step_types is None:
        from ..steps import STEP_TYPES

        step_types = set(STEP_TYPES)
    reg = ConfigRegistry(root=root, system=load_system(root), templates=load_templates(root))
    for project_dir in sorted(p for p in (root / "projects").iterdir() if (p / "project.yaml").exists()):
        try:
            project = load_project(project_dir, reg.templates, step_types)
        except ConfigError as e:
            reg.errors[project_dir.name] = str(e)
            continue
        if project.id in reg.projects:
            reg.errors[project_dir.name] = f"duplicate project id '{project.id}'"
            continue
        reg.projects[project.id] = project
    return reg
