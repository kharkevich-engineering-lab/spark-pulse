"""Canonical, versioned recipe schema owned by Spark Pulse.

Two formats coexist:

* **v1** — the upstream ``spark-vllm-docker`` shape: ``name`` / ``container`` /
  ``command``, with everything vLLM-specific baked into the command template.
  v1 stays valid forever; the importer (``recipe_import``) keeps it as-is.
* **v2** — engine-neutral ``params`` plus per-engine overrides under
  ``engines``, so one recipe can target vLLM, SGLang or a future engine.

The JSON Schema files under ``spark_pulse/schemas/`` are the published,
language-neutral contract (consumed by the ``spark-pulse-recipes`` repo). The
pydantic models here are the in-process parser and stay in step with them.

This module deliberately knows nothing about *rendering* a launch command;
it only models and validates data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "RecipeV1",
    "RecipeV2",
    "RecipeConstraints",
    "RecipeParams",
    "EngineSpec",
    "Recipe",
    "RecipeError",
    "RecipeValidationError",
    "parse_recipe",
    "to_v2",
    "validate_recipe_file",
    "validate_recipe_dir",
    "load_schema",
    "schema_registry",
    "detect_version",
    "SUPPORTED_VERSIONS",
]

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

SUPPORTED_VERSIONS = ("1", "2")

#: Engines a v1 recipe can run on. v1 command templates are vLLM flags.
V1_ENGINES = ["vllm"]


# ── Errors ───────────────────────────────────────────────────────────────────


class RecipeError(BaseModel):
    """A single validation problem, addressed by a dotted field path."""

    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}: {self.message}" if self.path else self.message


class RecipeValidationError(ValueError):
    """Raised when a recipe document does not match its schema version.

    Carries the individual ``errors`` so callers (CLI, importer, API) can
    report a field path per problem instead of one opaque string.
    """

    def __init__(
        self,
        errors: list[RecipeError],
        source: str | None = None,
    ) -> None:
        self.errors = errors
        self.source = source
        where = f" in {source}" if source else ""
        detail = "\n".join(f"  - {e}" for e in errors)
        super().__init__(f"Invalid recipe{where}:\n{detail}")

    def as_dicts(self) -> list[dict[str, str]]:
        """Return the errors as plain dicts, for JSON responses."""
        return [e.model_dump() for e in self.errors]


def _errors_from_pydantic(exc: ValidationError) -> list[RecipeError]:
    out: list[RecipeError] = []
    for err in exc.errors():
        path = ".".join(str(p) for p in err.get("loc", ()))
        out.append(RecipeError(path=path, message=err.get("msg", "invalid value")))
    return out


# ── Models ───────────────────────────────────────────────────────────────────


class RecipeV1(BaseModel):
    """The upstream spark-vllm-docker recipe format.

    ``container`` and ``command`` are required by the published schema but
    default here, so :func:`parse_recipe` can run in lenient mode over
    partially written user recipes without exploding. Strict mode (the
    default) enforces the required set.
    """

    model_config = ConfigDict(extra="allow")

    recipe_version: str = "1"
    name: str
    container: str = "vllm-node"
    command: str = ""
    model: str | None = None
    description: str = ""
    mods: list[str] = Field(default_factory=list)
    build_args: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, Any] = Field(default_factory=dict)
    solo_only: bool = False
    cluster_only: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "recipe_version" in data and data["recipe_version"] is not None:
                data["recipe_version"] = str(data["recipe_version"])
            for key in ("mods", "build_args"):
                value = data.get(key)
                if isinstance(value, str):
                    data[key] = [value]
        return data

    @property
    def engines(self) -> list[str]:
        """Engine names this recipe can run on (always vLLM for v1)."""
        return list(V1_ENGINES)


class RecipeConstraints(BaseModel):
    """Topology constraints enforced at plan time."""

    model_config = ConfigDict(extra="forbid")

    solo_only: bool = False
    cluster_only: bool = False
    min_nodes: int | None = Field(default=None, ge=1)


class RecipeParams(BaseModel):
    """Engine-neutral serving parameters, exposed in the deploy form."""

    model_config = ConfigDict(extra="allow")

    port: int | None = Field(default=None, ge=1, le=65535)
    host: str | None = None
    tensor_parallel: int | None = Field(default=None, ge=1)
    pipeline_parallel: int | None = Field(default=None, ge=1)
    gpu_memory_utilization: float | None = Field(default=None, gt=0, le=1)
    max_model_len: int | None = Field(default=None, ge=1)
    max_num_batched_tokens: int | None = Field(default=None, ge=1)
    max_num_seqs: int | None = Field(default=None, ge=1)

    def as_dict(self) -> dict[str, Any]:
        """Return the set parameters, dropping unset (``None``) ones."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


class EngineSpec(BaseModel):
    """Per-engine overrides inside a v2 recipe."""

    model_config = ConfigDict(extra="allow")

    image: str | None = None
    mods: list[str] = Field(default_factory=list)
    env: dict[str, Any] = Field(default_factory=dict)
    args: str | list[str] | None = None
    command: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if isinstance(data.get("mods"), str):
                data["mods"] = [data["mods"]]
        return data

    def args_string(self) -> str:
        """Return ``args`` as a single string regardless of how it was written."""
        if self.args is None:
            return ""
        if isinstance(self.args, str):
            return self.args
        return " ".join(self.args)


class RecipeV2(BaseModel):
    """The structured, multi-engine recipe format."""

    model_config = ConfigDict(extra="allow")

    recipe_version: str = "2"
    name: str
    model: str
    description: str = ""
    engine: str | None = None
    constraints: RecipeConstraints = Field(default_factory=RecipeConstraints)
    params: RecipeParams = Field(default_factory=RecipeParams)
    engines: dict[str, EngineSpec] = Field(default_factory=dict)
    command: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if "recipe_version" in data and data["recipe_version"] is not None:
                data["recipe_version"] = str(data["recipe_version"])
        return data

    @model_validator(mode="after")
    def _check_command_pins_engine(self) -> RecipeV2:
        if self.command and not self.engine:
            raise ValueError(
                "a top-level 'command' pins the recipe to one engine, "
                "so 'engine' is required alongside it"
            )
        if self.engine and self.engines and self.engine not in self.engines:
            raise ValueError(
                f"engine '{self.engine}' is not described under 'engines' "
                f"(has: {', '.join(sorted(self.engines))})"
            )
        return self

    def engine_names(self) -> list[str]:
        """Engine names this recipe supports, preferred engine first."""
        names = sorted(self.engines)
        if self.engine and self.engine not in names:
            names.insert(0, self.engine)
        elif self.engine:
            names.remove(self.engine)
            names.insert(0, self.engine)
        return names

    def engine_spec(self, engine: str | None = None) -> EngineSpec | None:
        """Return the spec for ``engine``, or for the recipe's default engine."""
        name = engine or self.engine
        if name is None:
            names = self.engine_names()
            name = names[0] if names else None
        if name is None:
            return None
        return self.engines.get(name)


Recipe = Union[RecipeV1, RecipeV2]


# ── Parsing ──────────────────────────────────────────────────────────────────


def detect_version(data: dict[str, Any]) -> str:
    """Return the declared recipe version, defaulting to ``"1"``."""
    raw = data.get("recipe_version")
    if raw is None:
        return "1"
    return str(raw).strip()


def _load_document(
    source: dict[str, Any] | str | Path,
) -> tuple[dict[str, Any], str | None]:
    """Normalise the accepted inputs to ``(mapping, source_label)``."""
    label: str | None = None
    if isinstance(source, Path):
        label = str(source)
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise RecipeValidationError(
                [RecipeError(path="", message=f"cannot read file: {exc}")], label
            ) from exc
        data = _parse_yaml(text, label)
    elif isinstance(source, str):
        data = _parse_yaml(source, None)
    elif isinstance(source, dict):
        data = source
    else:
        raise TypeError(f"unsupported recipe source type: {type(source).__name__}")

    if data is None:
        raise RecipeValidationError(
            [RecipeError(path="", message="recipe document is empty")], label
        )
    if not isinstance(data, dict):
        raise RecipeValidationError(
            [RecipeError(path="", message="recipe must be a YAML mapping")], label
        )
    return data, label


def _parse_yaml(text: str, label: str | None) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeValidationError(
            [RecipeError(path="", message=f"invalid YAML: {exc}")], label
        ) from exc


_V1_REQUIRED = ("name", "container", "command")
_V2_REQUIRED = ("name", "model")


def parse_recipe(
    source: dict[str, Any] | str | Path,
    *,
    strict: bool = True,
    source_label: str | None = None,
) -> Recipe:
    """Parse a recipe document and return the matching versioned model.

    ``source`` may be a mapping, a YAML string, or a :class:`~pathlib.Path`.
    Dispatch is on ``recipe_version`` (absent means ``"1"``).

    With ``strict=True`` (the default) the required-field set of the published
    JSON Schema is enforced. Lenient mode is what recipe *listing* uses, so a
    half-written user recipe still shows up in the UI.

    Raises :class:`RecipeValidationError` with per-field paths.
    """
    data, label = _load_document(source)
    label = source_label or label
    version = detect_version(data)

    if version not in SUPPORTED_VERSIONS:
        raise RecipeValidationError(
            [
                RecipeError(
                    path="recipe_version",
                    message=(
                        f"unsupported recipe version {version!r}; "
                        f"expected one of {', '.join(SUPPORTED_VERSIONS)}"
                    ),
                )
            ],
            label,
        )

    required = _V1_REQUIRED if version == "1" else _V2_REQUIRED
    if strict:
        missing = [
            RecipeError(path=key, message="field required")
            for key in required
            if not data.get(key)
        ]
        if missing:
            raise RecipeValidationError(missing, label)

    model_cls: type[BaseModel] = RecipeV1 if version == "1" else RecipeV2
    payload = dict(data)
    payload["recipe_version"] = version
    if version == "2" and not payload.get("model"):
        payload["model"] = ""
    try:
        return model_cls.model_validate(payload)  # type: ignore[return-value]
    except ValidationError as exc:
        raise RecipeValidationError(_errors_from_pydantic(exc), label) from exc


def to_v2(recipe: RecipeV1) -> RecipeV2:
    """Convert a v1 recipe into the equivalent v2 recipe.

    The v1 command template is vLLM-specific, so it becomes
    ``engines.vllm.command`` and the recipe is pinned to ``engine: vllm``.
    ``defaults`` become engine-neutral ``params`` (unknown keys are carried
    through), and the ``*_only`` flags become ``constraints``.
    """
    spec = EngineSpec(
        image=recipe.container or None,
        mods=list(recipe.mods),
        env=dict(recipe.env),
        command=recipe.command or None,
    )
    return RecipeV2(
        recipe_version="2",
        name=recipe.name,
        model=recipe.model or "",
        description=recipe.description,
        engine="vllm",
        constraints=RecipeConstraints(
            solo_only=recipe.solo_only,
            cluster_only=recipe.cluster_only,
        ),
        params=RecipeParams.model_validate(dict(recipe.defaults)),
        engines={"vllm": spec},
    )


# ── File / directory validation ──────────────────────────────────────────────


def validate_recipe_file(path: str | Path) -> dict[str, Any]:
    """Validate one recipe file.

    Returns ``{"path", "ok", "recipe_version", "name", "errors"}``; never
    raises for a bad recipe, so callers can report every file in a batch.
    """
    p = Path(path)
    result: dict[str, Any] = {
        "path": str(p),
        "ok": False,
        "recipe_version": None,
        "name": None,
        "errors": [],
    }
    try:
        recipe = parse_recipe(p, strict=True)
    except RecipeValidationError as exc:
        result["errors"] = exc.as_dicts()
        return result
    result["ok"] = True
    result["recipe_version"] = recipe.recipe_version
    result["name"] = recipe.name
    return result


def validate_recipe_dir(path: str | Path) -> list[dict[str, Any]]:
    """Validate every ``*.yaml`` / ``*.yml`` file under a directory tree."""
    root = Path(path)
    files: list[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        files.extend(root.rglob(pattern))
    return [validate_recipe_file(f) for f in sorted(set(files))]


# ── Published JSON Schemas ───────────────────────────────────────────────────

_SCHEMA_FILES = {
    "recipe": "recipe.schema.json",
    "1": "recipe-v1.schema.json",
    "2": "recipe-v2.schema.json",
}


def load_schema(name: str = "recipe") -> dict[str, Any]:
    """Load a published JSON Schema by name (``"recipe"``, ``"1"`` or ``"2"``)."""
    filename = _SCHEMA_FILES.get(str(name))
    if filename is None:
        raise KeyError(f"unknown recipe schema {name!r}")
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


def schema_registry() -> dict[str, dict[str, Any]]:
    """Return every published schema keyed by its ``$id``.

    Useful for wiring a ``jsonschema`` resolver, since the combined schema
    refers to the per-version files by ``$id``.
    """
    out: dict[str, dict[str, Any]] = {}
    for name in _SCHEMA_FILES:
        schema = load_schema(name)
        out[schema["$id"]] = schema
    return out
