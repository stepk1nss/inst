"""Configuration schema: system, scenario templates, projects.

The core only understands the concepts defined here (fields, steps, secrecy,
platform targets). Nothing project-specific lives in code.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PLATFORMS = ("telegram", "instagram", "tiktok", "youtube")
PlatformName = Literal["telegram", "instagram", "tiktok", "youtube"]
FieldType = Literal["string", "boolean", "integer", "number", "array", "platform_texts"]
PublishMode = Literal["draft", "confirm", "auto"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- system


class ModelSpec(Strict):
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = 16000


class ModelsConfig(Strict):
    generate: ModelSpec
    classify: ModelSpec
    judge: ModelSpec


class TTSConfig(Strict):
    model: str = "eleven_multilingual_v2"
    fallback_voice_id: str = ""


class RenderConfig(Strict):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    zoom: float = 1.06
    tail_seconds: float = 1.0
    blur: int = 40


class ImageConfig(Strict):
    max_side: int = 2048
    jpeg_quality: int = 92


class SystemConfig(Strict):
    timezone: str = "UTC"
    models: ModelsConfig
    tts: TTSConfig = TTSConfig()
    render: RenderConfig = RenderConfig()
    image: ImageConfig = ImageConfig()


# --------------------------------------------------------------------------- scenario building blocks


class FieldSpec(Strict):
    type: FieldType = "string"
    items: FieldType | None = None
    desc: str = ""
    # Secret fields are invisible to steps without sees.secrets and to platforms
    # without reveal_secrets. Enforced by the core, not by prompts.
    secret: bool = False
    # llm: produced by a generate step; caption: may only be taken from the user's
    # caption (never invented); enrich.<provider>: reserved for future external sources.
    source: str = "llm"

    @model_validator(mode="after")
    def _check(self) -> "FieldSpec":
        if self.type == "array" and self.items is None:
            self.items = "string"
        if not (self.source in ("llm", "caption") or self.source.startswith("enrich.")):
            raise ValueError(f"unknown field source '{self.source}'")
        return self


class Sees(Strict):
    media: bool = False
    caption: bool = False
    fields: list[str] | Literal["all"] = Field(default_factory=list)
    secrets: bool = False


class StepSpec(Strict):
    id: str
    use: str
    sees: Sees = Sees()
    produces: list[str] = Field(default_factory=list)
    instructions: str = ""
    input: str | None = None  # tts: field with the text to speak
    params: dict[str, Any] = Field(default_factory=dict)


class CheckSpec(Strict):
    type: Literal["no_secret_leak", "block_auto_if"]
    # no_secret_leak
    secrets: list[str] = Field(default_factory=list)
    options_field: str | None = None
    index_field: str | None = None
    judge: bool = False
    # block_auto_if
    field: str | None = None
    equals: Any = None
    message: str = ""


class PlatformDefault(Strict):
    reveal_secrets: bool = False
    template: str | None = None  # Jinja2 template over fields
    text_from: str | None = None  # "platform_texts.<platform>" or a field name

    @model_validator(mode="after")
    def _one_source(self) -> "PlatformDefault":
        if bool(self.template) == bool(self.text_from):
            raise ValueError("platform default needs exactly one of 'template' or 'text_from'")
        return self


class ScenarioTemplate(Strict):
    id: str
    description: str = ""
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    steps: list[StepSpec] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    platform_defaults: dict[PlatformName, PlatformDefault] = Field(default_factory=dict)


class ScenarioOverrides(Strict):
    fields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    platforms: dict[PlatformName, dict[str, Any]] = Field(default_factory=dict)


class ScenarioConfig(Strict):
    """Scenario as written in project.yaml (before resolving `extends`)."""

    id: str
    title: str
    tag: str | None = None
    when: str = ""
    extends: str | None = None
    knowledge: list[str] = Field(default_factory=list)
    instructions: str = ""
    overrides: ScenarioOverrides = ScenarioOverrides()
    checks_add: list[CheckSpec] = Field(default_factory=list)
    # Inline definition (when not extending a template)
    fields: dict[str, FieldSpec] = Field(default_factory=dict)
    steps: list[StepSpec] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    platform_defaults: dict[PlatformName, PlatformDefault] = Field(default_factory=dict)


class Scenario(Strict):
    """Fully resolved scenario used by the pipeline."""

    id: str
    title: str
    tag: str | None = None
    when: str = ""
    template: str | None = None
    instructions: str = ""
    knowledge_text: str = ""
    fields: dict[str, FieldSpec]
    steps: list[StepSpec]
    checks: list[CheckSpec]
    platform_defaults: dict[PlatformName, PlatformDefault]

    def secret_fields(self) -> set[str]:
        return {name for name, f in self.fields.items() if f.secret}

    def step(self, step_id: str) -> StepSpec:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)


# --------------------------------------------------------------------------- project


class BrandConfig(Strict):
    file: str | None = None
    text: str = ""
    examples_dir: str | None = None


class KnowledgeConfig(Strict):
    files: list[str] = Field(default_factory=list)


class VoiceOption(Strict):
    id: str
    name: str
    provider_voice_id: str = ""
    note: str = ""


class VoiceConfig(Strict):
    enabled: bool = True
    default: str | None = None
    speed: float = 1.0
    allowed: list[VoiceOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_known(self) -> "VoiceConfig":
        ids = [v.id for v in self.allowed]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate voice ids")
        if self.enabled and self.allowed:
            if self.default is None:
                self.default = ids[0]
            elif self.default not in ids:
                raise ValueError(f"voice.default '{self.default}' is not in voice.allowed")
        return self

    def get(self, voice_id: str) -> VoiceOption | None:
        return next((v for v in self.allowed if v.id == voice_id), None)


class PlatformTarget(Strict):
    enabled: bool = True
    connection: str
    # photo | video | reels | shorts — what media the platform receives
    format: Literal["photo", "video", "reels", "shorts"] = "photo"
    mode: Literal["direct", "draft"] = "direct"  # tiktok: draft until API audit
    visibility: Literal["public", "unlisted", "private"] = "public"  # youtube
    hashtags_fixed: list[str] = Field(default_factory=list)
    cta: str = ""

    @property
    def wants_video(self) -> bool:
        return self.format in ("video", "reels", "shorts")


class PublishingConfig(Strict):
    mode: PublishMode = "confirm"


class ScenarioSelection(Strict):
    auto: bool = True
    default: str


class ProjectConfig(Strict):
    """Project as written in project.yaml."""

    id: str
    name: str
    emoji: str = ""
    tag: str | None = None
    language: str = "ru"
    commercial: bool = False
    brand: BrandConfig = BrandConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    voice: VoiceConfig = VoiceConfig(enabled=False)
    platforms: dict[PlatformName, PlatformTarget] = Field(default_factory=dict)
    publishing: PublishingConfig = PublishingConfig()
    scenario_selection: ScenarioSelection
    scenarios: list[ScenarioConfig]


class Project(Strict):
    """Fully resolved project: scenarios resolved, prompt files read."""

    id: str
    name: str
    emoji: str = ""
    tag: str | None = None
    language: str
    commercial: bool
    brand_text: str
    knowledge_text: str
    examples: list[str]
    voice: VoiceConfig
    platforms: dict[PlatformName, PlatformTarget]
    publishing: PublishingConfig
    scenario_selection: ScenarioSelection
    scenarios: dict[str, Scenario]
    warnings: list[str] = Field(default_factory=list)

    def enabled_platforms(self) -> dict[str, PlatformTarget]:
        return {k: v for k, v in self.platforms.items() if v.enabled}

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip()
