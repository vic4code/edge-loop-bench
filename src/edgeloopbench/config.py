"""Experiment manifest parsing and scientific-invariant validation."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
MAX_MANIFEST_FILE_BYTES = 4 * 1024 * 1024
MAX_PLANNED_RUNS = 250_000
TRACKS = frozenset({"effectiveness", "serving", "deployment"})
STRATEGIES = frozenset({"direct", "bounded_retry", "maker_verifier"})
BACKENDS = frozenset({"ollama", "vllm-metal", "mlx-lm"})
UNPINNED_VALUES = frozenset(
    {
        "",
        "candidate",
        "dev",
        "development",
        "head",
        "latest",
        "main",
        "master",
        "nightly",
        "stable",
        "tbd",
        "trunk",
        "unknown",
        "unpinned",
    }
)
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
COMMIT_PATTERN = re.compile(r"^(?:commit[-:]?)?[0-9a-f]{7,64}$", re.IGNORECASE)
DIGEST_PATTERN = re.compile(r"^[a-z0-9_+.-]+:[0-9a-f]{32,128}$", re.IGNORECASE)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)
VERSION_PATTERN = re.compile(
    r"^v?\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
SECRET_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIALS?|PAT|AUTHORIZATION|BEARER)"
    r"(?:_|$)|(?:^|_)(?:API|PRIVATE|ACCESS)_KEY(?:_|$)"
)


class ValidationError(ValueError):
    """Raised when an experiment manifest violates its data contract."""


@dataclass(frozen=True)
class ModelConfig:
    id: str
    revision: str
    artifact_sha256: str
    weight_quantization: str
    context_limit_tokens: int


@dataclass(frozen=True)
class BackendConfig:
    name: str
    version: str
    artifact_sha256: str
    command: tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class GenerationConfig:
    thinking: bool
    temperature: float
    edit_schema_revision: str
    controller_revision: str


@dataclass(frozen=True)
class LogicalBudget:
    prompt_tokens: int
    completion_tokens: int
    model_calls: int
    tool_calls: int
    public_test_runs: int
    per_call_context_tokens: int


@dataclass(frozen=True)
class RequestShape:
    name: str
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class MeasurementConfig:
    warmups: int
    repetitions: int
    concurrency: int


@dataclass(frozen=True)
class ExperimentPlan:
    schema_version: int
    id: str
    track: str
    draft: bool
    model: ModelConfig
    backend: BackendConfig
    generation: GenerationConfig | None = None
    tasks: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    seeds: tuple[int, ...] = ()
    budgets: Mapping[str, LogicalBudget] | None = None
    request_shapes: tuple[RequestShape, ...] = ()
    measurement: MeasurementConfig | None = None
    physical_budget: Mapping[str, float] | None = None
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.budgets is None:
            object.__setattr__(self, "budgets", {})

    @property
    def run_count(self) -> int:
        if self.track == "serving":
            assert self.measurement is not None
            return len(self.request_shapes) * self.measurement.repetitions
        return (
            len(self.tasks)
            * len(self.strategies)
            * len(self.seeds)
            * len(self.budgets or {})
        )

    def summary(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "track": self.track,
            "draft": self.draft,
            "model": self.model.id,
            "model_revision": self.model.revision,
            "model_artifact_sha256": self.model.artifact_sha256,
            "backend": self.backend.name,
            "backend_version": self.backend.version,
            "backend_artifact_sha256": self.backend.artifact_sha256,
            "backend_command": list(self.backend.command),
            "planned_runs": self.run_count,
            "manifest_sha256": self.manifest_sha256,
        }
        if self.track == "serving":
            result["request_shapes"] = [shape.name for shape in self.request_shapes]
        else:
            result["tasks"] = len(self.tasks)
            result["strategies"] = list(self.strategies)
            result["seeds"] = list(self.seeds)
            result["budget_tiers"] = list((self.budgets or {}).keys())
            assert self.generation is not None
            result["thinking"] = self.generation.thinking
            result["temperature"] = self.generation.temperature
            result["edit_schema_revision"] = self.generation.edit_schema_revision
            result["controller_revision"] = self.generation.controller_revision
        return result


def load_experiment(path: str | Path) -> ExperimentPlan:
    """Load and validate an experiment TOML file."""

    source = Path(path)
    try:
        with source.open("rb") as handle:
            payload = handle.read(MAX_MANIFEST_FILE_BYTES + 1)
    except FileNotFoundError as error:
        raise ValidationError(f"experiment manifest not found: {source}") from error
    except OSError as error:
        raise ValidationError(
            f"cannot read experiment manifest {source}: {error}"
        ) from error
    if len(payload) > MAX_MANIFEST_FILE_BYTES:
        raise ValidationError(
            f"experiment manifest {source} exceeds the "
            f"{MAX_MANIFEST_FILE_BYTES}-byte safety limit"
        )
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValidationError(
            f"experiment manifest is not valid UTF-8: {source}"
        ) from error
    except RecursionError as error:
        raise ValidationError(
            f"experiment manifest nesting is too deep: {source}"
        ) from error
    except tomllib.TOMLDecodeError as error:
        raise ValidationError(f"invalid TOML in {source}: {error}") from error
    except ValueError as error:
        raise ValidationError(f"invalid TOML value in {source}: {error}") from error
    return validate_experiment(
        raw,
        source=str(source),
        manifest_sha256=sha256(payload).hexdigest(),
    )


def validate_experiment(
    raw: Mapping[str, Any],
    *,
    source: str = "<mapping>",
    manifest_sha256: str | None = None,
) -> ExperimentPlan:
    """Validate an already parsed experiment mapping."""

    if not isinstance(raw, Mapping):
        raise ValidationError(f"{source}: top level must be a table")
    if "strategy_budgets" in raw:
        raise ValidationError(
            f"{source}: effectiveness plans must use shared budgets; strategy_budgets is not allowed"
        )

    allowed = {
        "schema_version",
        "id",
        "track",
        "draft",
        "model",
        "backend",
        "generation",
        "tasks",
        "strategies",
        "seeds",
        "budgets",
        "request_shapes",
        "measurement",
        "physical_budget",
        "notes",
    }
    _reject_unknown(raw, allowed, source)

    schema_version = _integer(raw, "schema_version", source)
    if schema_version != SCHEMA_VERSION:
        raise ValidationError(
            f"{source}: schema_version must be {SCHEMA_VERSION}, got {schema_version}"
        )
    experiment_id = _identifier(raw, "id", source)
    track = _string(raw, "track", source)
    if track not in TRACKS:
        raise ValidationError(
            f"{source}: track must be one of {sorted(TRACKS)}, got {track!r}"
        )
    draft = _boolean(raw, "draft", source)
    model = _parse_model(_table(raw, "model", source), source)
    backend = _parse_backend(_table(raw, "backend", source), source)

    if not draft:
        if not _is_immutable_pin(model.revision):
            raise ValidationError(
                f"{source}: model.revision must be pinned for a publishable plan"
            )
        if not SHA256_PATTERN.fullmatch(model.artifact_sha256):
            raise ValidationError(
                f"{source}: model.artifact_sha256 must be a SHA-256 digest for a publishable plan"
            )
        if not _is_immutable_pin(backend.version):
            raise ValidationError(
                f"{source}: backend.version must be pinned for a publishable plan"
            )
        if not SHA256_PATTERN.fullmatch(backend.artifact_sha256):
            raise ValidationError(
                f"{source}: backend.artifact_sha256 must be a SHA-256 digest for a publishable plan"
            )

    if track == "serving":
        if "generation" in raw:
            raise ValidationError(f"{source}: serving track cannot define generation")
        plan = _parse_serving(
            raw,
            source=source,
            schema_version=schema_version,
            experiment_id=experiment_id,
            draft=draft,
            model=model,
            backend=backend,
            manifest_sha256=manifest_sha256,
        )
    else:
        generation = _parse_generation(_table(raw, "generation", source), source)
        plan = _parse_agent_track(
            raw,
            source=source,
            schema_version=schema_version,
            experiment_id=experiment_id,
            track=track,
            draft=draft,
            model=model,
            backend=backend,
            generation=generation,
            manifest_sha256=manifest_sha256,
        )
    if plan.run_count > MAX_PLANNED_RUNS:
        raise ValidationError(
            f"{source}: planned run count {plan.run_count} exceeds safety limit "
            f"{MAX_PLANNED_RUNS}"
        )
    return plan


def _parse_model(raw: Mapping[str, Any], source: str) -> ModelConfig:
    field = f"{source}: model"
    _reject_unknown(
        raw,
        {
            "id",
            "revision",
            "artifact_sha256",
            "weight_quantization",
            "context_limit_tokens",
        },
        field,
    )
    return ModelConfig(
        id=_string(raw, "id", field),
        revision=_string(raw, "revision", field),
        artifact_sha256=_string(raw, "artifact_sha256", field),
        weight_quantization=_string(raw, "weight_quantization", field),
        context_limit_tokens=_positive_integer(raw, "context_limit_tokens", field),
    )


def _parse_backend(raw: Mapping[str, Any], source: str) -> BackendConfig:
    field = f"{source}: backend"
    _reject_unknown(
        raw, {"name", "version", "artifact_sha256", "command", "environment"}, field
    )
    name = _string(raw, "name", field)
    if name not in BACKENDS:
        raise ValidationError(
            f"{field}.name must be one of {sorted(BACKENDS)}, got {name!r}"
        )
    command = _string_array(raw, "command", field)
    for argument in command:
        if _looks_secret_bearing_argument(argument):
            raise ValidationError(
                f"{field}.command contains secret-bearing argument {argument!r}; "
                "use a local non-secret configuration instead"
            )
    environment_raw = _table(raw, "environment", field)
    environment: dict[str, str] = {}
    for key, value in environment_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValidationError(
                f"{field}.environment contains invalid variable name {key!r}"
            )
        if not isinstance(value, str):
            raise ValidationError(f"{field}.environment.{key} must be a string")
        if _looks_secret_bearing_name(key):
            raise ValidationError(
                f"{field}.environment.{key} looks secret-bearing and must not be stored in a manifest"
            )
        environment[key] = value
    _validate_backend_loopback(name, command, environment, field)
    return BackendConfig(
        name=name,
        version=_string(raw, "version", field),
        artifact_sha256=_string(raw, "artifact_sha256", field),
        command=command,
        environment=environment,
    )


def _parse_generation(raw: Mapping[str, Any], source: str) -> GenerationConfig:
    field = f"{source}: generation"
    _reject_unknown(
        raw,
        {"thinking", "temperature", "edit_schema_revision", "controller_revision"},
        field,
    )
    temperature = raw.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise ValidationError(f"{field}: temperature must be a number")
    temperature = float(temperature)
    if not math.isfinite(temperature) or temperature < 0:
        raise ValidationError(f"{field}: temperature must be finite and nonnegative")
    controller_revision = _string(raw, "controller_revision", field)
    if not _is_immutable_pin(controller_revision):
        raise ValidationError(f"{field}: controller_revision must be immutable")
    return GenerationConfig(
        thinking=_boolean(raw, "thinking", field),
        temperature=temperature,
        edit_schema_revision=_identifier(raw, "edit_schema_revision", field),
        controller_revision=controller_revision,
    )


def _parse_agent_track(
    raw: Mapping[str, Any],
    *,
    source: str,
    schema_version: int,
    experiment_id: str,
    track: str,
    draft: bool,
    model: ModelConfig,
    backend: BackendConfig,
    generation: GenerationConfig,
    manifest_sha256: str | None,
) -> ExperimentPlan:
    if "request_shapes" in raw or "measurement" in raw:
        raise ValidationError(
            f"{source}: {track} track cannot define serving request shapes"
        )

    tasks = _unique_string_list(raw, "tasks", source)
    strategies = _unique_string_list(raw, "strategies", source)
    if len(strategies) < 2:
        raise ValidationError(
            f"{source}: strategies must contain at least two comparison arms"
        )
    unsupported = sorted(set(strategies) - STRATEGIES)
    if unsupported:
        raise ValidationError(
            f"{source}: unsupported strategies: {', '.join(unsupported)}"
        )
    seeds = _unique_integer_list(raw, "seeds", source)

    budget_tables = _table(raw, "budgets", source)
    if not budget_tables:
        raise ValidationError(
            f"{source}: budgets must contain at least one shared tier"
        )
    budgets: dict[str, LogicalBudget] = {}
    for name, value in budget_tables.items():
        if not isinstance(name, str) or not IDENTIFIER_PATTERN.fullmatch(name):
            raise ValidationError(f"{source}: invalid budget tier name {name!r}")
        if not isinstance(value, Mapping):
            raise ValidationError(f"{source}: budgets.{name} must be a table")
        budget = _parse_budget(value, f"{source}: budgets.{name}")
        if budget.per_call_context_tokens > model.context_limit_tokens:
            raise ValidationError(
                f"{source}: budgets.{name}.per_call_context_tokens exceeds the model context limit"
            )
        budgets[name] = budget

    physical_budget: Mapping[str, float] | None = None
    if track == "deployment":
        physical_budget = _parse_physical_budget(
            _table(raw, "physical_budget", source), source
        )
    elif "physical_budget" in raw:
        raise ValidationError(
            f"{source}: physical_budget is only valid for the deployment track"
        )

    return ExperimentPlan(
        schema_version=schema_version,
        id=experiment_id,
        track=track,
        draft=draft,
        model=model,
        backend=backend,
        generation=generation,
        tasks=tasks,
        strategies=strategies,
        seeds=seeds,
        budgets=budgets,
        physical_budget=physical_budget,
        manifest_sha256=manifest_sha256,
    )


def _parse_budget(raw: Mapping[str, Any], source: str) -> LogicalBudget:
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "model_calls",
        "tool_calls",
        "public_test_runs",
        "per_call_context_tokens",
    )
    _reject_unknown(raw, set(fields), source)
    return LogicalBudget(
        **{field: _positive_integer(raw, field, source) for field in fields}
    )


def _parse_physical_budget(raw: Mapping[str, Any], source: str) -> Mapping[str, float]:
    field = f"{source}: physical_budget"
    fields = ("max_wall_seconds", "max_energy_joules")
    _reject_unknown(raw, set(fields), field)
    result = {
        name: _positive_number(raw[name], f"{field}.{name}")
        for name in fields
        if name in raw
    }
    if not result:
        raise ValidationError(
            f"{field} must define max_wall_seconds or max_energy_joules"
        )
    return result


def _parse_serving(
    raw: Mapping[str, Any],
    *,
    source: str,
    schema_version: int,
    experiment_id: str,
    draft: bool,
    model: ModelConfig,
    backend: BackendConfig,
    manifest_sha256: str | None,
) -> ExperimentPlan:
    forbidden = sorted(
        {"tasks", "strategies", "seeds", "budgets", "physical_budget"} & raw.keys()
    )
    if forbidden:
        raise ValidationError(
            f"{source}: serving track cannot define {', '.join(forbidden)}"
        )

    raw_shapes = raw.get("request_shapes")
    if not isinstance(raw_shapes, list) or not raw_shapes:
        raise ValidationError(
            f"{source}: request_shapes must be a non-empty array of tables"
        )
    shapes: list[RequestShape] = []
    for index, value in enumerate(raw_shapes):
        field = f"{source}: request_shapes[{index}]"
        if not isinstance(value, Mapping):
            raise ValidationError(f"{field} must be a table")
        _reject_unknown(value, {"name", "prompt_tokens", "completion_tokens"}, field)
        shape = RequestShape(
            name=_identifier(value, "name", field),
            prompt_tokens=_positive_integer(value, "prompt_tokens", field),
            completion_tokens=_positive_integer(value, "completion_tokens", field),
        )
        if shape.prompt_tokens + shape.completion_tokens > model.context_limit_tokens:
            raise ValidationError(f"{field} exceeds the model context limit")
        shapes.append(shape)
    if len({shape.name for shape in shapes}) != len(shapes):
        raise ValidationError(f"{source}: request shape names must be unique")

    measurement_raw = _table(raw, "measurement", source)
    field = f"{source}: measurement"
    _reject_unknown(measurement_raw, {"warmups", "repetitions", "concurrency"}, field)
    warmups = _integer(measurement_raw, "warmups", field)
    if warmups < 0:
        raise ValidationError(f"{field}.warmups must be non-negative")
    measurement = MeasurementConfig(
        warmups=warmups,
        repetitions=_positive_integer(measurement_raw, "repetitions", field),
        concurrency=_positive_integer(measurement_raw, "concurrency", field),
    )
    return ExperimentPlan(
        schema_version=schema_version,
        id=experiment_id,
        track="serving",
        draft=draft,
        model=model,
        backend=backend,
        request_shapes=tuple(shapes),
        measurement=measurement,
        manifest_sha256=manifest_sha256,
    )


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], source: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValidationError(f"{source}: unknown fields: {', '.join(unknown)}")


def _table(raw: Mapping[str, Any], key: str, source: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise ValidationError(f"{source}: {key} must be a table")
    return value


def _string(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{source}: {key} must be a non-empty string")
    return value.strip()


def _identifier(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = _string(raw, key, source)
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValidationError(
            f"{source}: {key} must be a lowercase stable identifier, got {value!r}"
        )
    return value


def _boolean(raw: Mapping[str, Any], key: str, source: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValidationError(f"{source}: {key} must be a boolean")
    return value


def _integer(raw: Mapping[str, Any], key: str, source: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{source}: {key} must be an integer")
    return value


def _positive_integer(raw: Mapping[str, Any], key: str, source: str) -> int:
    value = _integer(raw, key, source)
    if value <= 0:
        raise ValidationError(f"{source}: {key} must be positive")
    return value


def _positive_number(value: Any, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{source} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValidationError(f"{source} must be a finite number") from error
    if not math.isfinite(number):
        raise ValidationError(f"{source} must be finite")
    if number <= 0:
        raise ValidationError(f"{source} must be positive")
    return number


def _unique_string_list(
    raw: Mapping[str, Any], key: str, source: str
) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{source}: {key} must be a non-empty array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{source}: {key} must contain non-empty strings")
    items = tuple(item.strip() for item in value)
    if len(set(items)) != len(items):
        raise ValidationError(f"{source}: {key} entries must be unique")
    return items


def _unique_integer_list(
    raw: Mapping[str, Any], key: str, source: str
) -> tuple[int, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{source}: {key} must be a non-empty array")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in value
    ):
        raise ValidationError(f"{source}: {key} must contain non-negative integers")
    items = tuple(value)
    if len(set(items)) != len(items):
        raise ValidationError(f"{source}: {key} entries must be unique")
    return items


def _string_array(raw: Mapping[str, Any], key: str, source: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{source}: {key} must be a non-empty array of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValidationError(f"{source}: {key} must contain non-empty strings")
    return tuple(value)


def _validate_backend_loopback(
    name: str,
    command: tuple[str, ...],
    environment: Mapping[str, str],
    source: str,
) -> None:
    if name == "ollama":
        host = environment.get("OLLAMA_HOST")
        if host is None or not _is_loopback_host(host):
            raise ValidationError(
                f"{source}.environment.OLLAMA_HOST must use a loopback address"
            )
        return
    host_argument: str | None = None
    for index, argument in enumerate(command):
        if argument == "--host" and index + 1 < len(command):
            host_argument = command[index + 1]
        elif argument.startswith("--host="):
            host_argument = argument.split("=", 1)[1]
    if name in {"vllm-metal", "mlx-lm"} and host_argument is None:
        raise ValidationError(f"{source}.command must set --host to a loopback address")
    if host_argument is not None and not _is_loopback_host(host_argument):
        raise ValidationError(f"{source}.command --host must use a loopback address")


def _is_loopback_host(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return False

    if "://" in normalized:
        try:
            parsed = urlsplit(normalized)
            if parsed.scheme.lower() not in {"http", "https"}:
                return False
            if parsed.username is not None or parsed.password is not None:
                return False
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                return False
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return False
        if host is None or (port is not None and not 1 <= port <= 65535):
            return False
    else:
        if any(character in normalized for character in "/@?#"):
            return False
        bracketed = re.fullmatch(r"\[([^]]+)](?::([0-9]+))?", normalized)
        if bracketed:
            host, raw_port = bracketed.groups()
            if raw_port is not None and not _is_valid_port(raw_port):
                return False
        elif normalized.count(":") == 1:
            host, raw_port = normalized.rsplit(":", 1)
            if not _is_valid_port(raw_port):
                return False
        else:
            host = normalized

    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _is_valid_port(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,5}", value)) and 1 <= int(value) <= 65535


def _looks_secret_bearing_name(value: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    return bool(SECRET_NAME_PATTERN.search(normalized))


def _looks_secret_bearing_argument(argument: str) -> bool:
    candidate = ""
    if argument.startswith("-"):
        candidate = argument.lstrip("-").split("=", 1)[0]
    elif "=" in argument:
        candidate = argument.split("=", 1)[0]
    if candidate and _looks_secret_bearing_name(candidate):
        return True
    return bool(re.search(r"(?:authorization\s*:|bearer\s+)", argument, re.IGNORECASE))


def _is_immutable_pin(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in UNPINNED_VALUES:
        return False
    if normalized.startswith(("refs/heads/", "origin/", "upstream/")):
        return False
    return bool(
        COMMIT_PATTERN.fullmatch(normalized)
        or DIGEST_PATTERN.fullmatch(normalized)
        or VERSION_PATTERN.fullmatch(value.strip())
    )
