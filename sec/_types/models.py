"""模型元数据类型及其 camelCase 序列化格式。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .json import JSONObject, JSONValue

ThinkingLevel: TypeAlias = Literal[
    "off", "minimal", "low", "medium", "high", "xhigh", "max"
]
ModelInputModality: TypeAlias = Literal["text", "image"]
_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)


@dataclass
class ModelCostRates:
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass
class ModelCostTier(ModelCostRates):
    input_tokens_above: float


@dataclass
class ModelCost(ModelCostRates):
    tiers: list[ModelCostTier] | None = None


@dataclass
class ModelPromptCache:
    short: float | None = None
    long: float | None = None


@dataclass
class ModelImageResizeOptions:
    max_width: float | None = None
    max_height: float | None = None
    max_bytes: float | None = None
    jpeg_quality: float | None = None


@dataclass
class ModelImageInputLimits:
    resize: ModelImageResizeOptions | None = None
    max_per_message: float | None = None
    max_per_request: float | None = None


@dataclass
class ModelInputLimits:
    max_request_bytes: float | None = None
    images: ModelImageInputLimits | None = None


@dataclass
class Model:
    id: str
    name: str
    api: str
    provider: str
    base_url: str
    reasoning: bool
    input: list[ModelInputModality]
    cost: ModelCost
    context_window: float
    max_tokens: float
    thinking_level_map: dict[ThinkingLevel, str | None] | None = None
    input_limits: ModelInputLimits | None = None
    sampling_params: JSONObject | None = None
    headers: dict[str, str] | None = None
    compat: JSONObject | None = None
    prompt_cache: ModelPromptCache | None = None


def model_to_dict(model: Model) -> dict[str, Any]:
    """Serialize model metadata with the SDK's camelCase field names."""
    result: dict[str, Any] = {
        "id": model.id,
        "name": model.name,
        "api": model.api,
        "provider": model.provider,
        "baseUrl": model.base_url,
        "reasoning": model.reasoning,
        "input": list(model.input),
        "cost": _cost_to_dict(model.cost),
        "contextWindow": model.context_window,
        "maxTokens": model.max_tokens,
    }
    if model.thinking_level_map is not None:
        result["thinkingLevelMap"] = dict(model.thinking_level_map)
    if model.input_limits is not None:
        result["inputLimits"] = _input_limits_to_dict(model.input_limits)
    if model.sampling_params is not None:
        result["samplingParams"] = dict(model.sampling_params)
    if model.headers is not None:
        result["headers"] = dict(model.headers)
    if model.compat is not None:
        result["compat"] = dict(model.compat)
    if model.prompt_cache is not None:
        result["promptCache"] = _prompt_cache_to_dict(model.prompt_cache)
    return result


def model_from_dict(data: Mapping[str, Any]) -> Model:
    """Parse model metadata using the SDK's camelCase field names."""
    model_input = _required_list(data, "input")
    if any(item not in ("text", "image") for item in model_input):
        raise ValueError("input entries must be 'text' or 'image'")

    thinking_level_map = None
    if "thinkingLevelMap" in data:
        raw_map = _mapping(data["thinkingLevelMap"], "thinkingLevelMap")
        thinking_level_map = {}
        for key, value in raw_map.items():
            if key not in _THINKING_LEVELS:
                raise ValueError(f"unsupported thinking level {key!r}")
            if value is not None and not isinstance(value, str):
                raise ValueError("thinkingLevelMap values must be strings or null")
            thinking_level_map[key] = value

    return Model(
        id=_required_str(data, "id"),
        name=_required_str(data, "name"),
        api=_required_str(data, "api"),
        provider=_required_str(data, "provider"),
        base_url=_required_str(data, "baseUrl"),
        reasoning=_required_bool(data, "reasoning"),
        input=list(model_input),
        cost=_cost_from_dict(_required_mapping(data, "cost")),
        context_window=_required_number(data, "contextWindow"),
        max_tokens=_required_number(data, "maxTokens"),
        thinking_level_map=thinking_level_map,
        input_limits=(
            _input_limits_from_dict(_mapping(data["inputLimits"], "inputLimits"))
            if "inputLimits" in data
            else None
        ),
        sampling_params=(
            _json_object(data["samplingParams"], "samplingParams")
            if "samplingParams" in data
            else None
        ),
        headers=(
            _string_dict(data["headers"], "headers") if "headers" in data else None
        ),
        compat=(
            _json_object(data["compat"], "compat") if "compat" in data else None
        ),
        prompt_cache=(
            _prompt_cache_from_dict(_mapping(data["promptCache"], "promptCache"))
            if "promptCache" in data
            else None
        ),
    )


def _cost_from_dict(data: Mapping[str, Any]) -> ModelCost:
    tiers = None
    if "tiers" in data:
        raw_tiers = _required_list(data, "tiers")
        tiers = [_cost_tier_from_dict(_mapping(tier, "tiers[]")) for tier in raw_tiers]
    return ModelCost(**_cost_rates_from_dict(data), tiers=tiers)


def _cost_tier_from_dict(data: Mapping[str, Any]) -> ModelCostTier:
    return ModelCostTier(
        **_cost_rates_from_dict(data),
        input_tokens_above=_required_number(data, "inputTokensAbove"),
    )


def _cost_rates_from_dict(data: Mapping[str, Any]) -> dict[str, float]:
    return {
        "input": _required_number(data, "input"),
        "output": _required_number(data, "output"),
        "cache_read": _required_number(data, "cacheRead"),
        "cache_write": _required_number(data, "cacheWrite"),
    }


def _cost_to_dict(cost: ModelCostRates) -> dict[str, Any]:
    result: dict[str, Any] = {
        "input": cost.input,
        "output": cost.output,
        "cacheRead": cost.cache_read,
        "cacheWrite": cost.cache_write,
    }
    if isinstance(cost, ModelCostTier):
        result["inputTokensAbove"] = cost.input_tokens_above
    if isinstance(cost, ModelCost) and cost.tiers is not None:
        result["tiers"] = [_cost_to_dict(tier) for tier in cost.tiers]
    return result


def _prompt_cache_from_dict(data: Mapping[str, Any]) -> ModelPromptCache:
    return ModelPromptCache(
        short=_optional_number(data, "short"),
        long=_optional_number(data, "long"),
    )


def _prompt_cache_to_dict(cache: ModelPromptCache) -> dict[str, Any]:
    return _present_numbers({"short": cache.short, "long": cache.long})


def _input_limits_from_dict(data: Mapping[str, Any]) -> ModelInputLimits:
    return ModelInputLimits(
        max_request_bytes=_optional_number(data, "maxRequestBytes"),
        images=(
            _image_limits_from_dict(_mapping(data["images"], "images"))
            if "images" in data
            else None
        ),
    )


def _input_limits_to_dict(limits: ModelInputLimits) -> dict[str, Any]:
    result = _present_numbers({"maxRequestBytes": limits.max_request_bytes})
    if limits.images is not None:
        result["images"] = _image_limits_to_dict(limits.images)
    return result


def _image_limits_from_dict(data: Mapping[str, Any]) -> ModelImageInputLimits:
    return ModelImageInputLimits(
        resize=(
            _resize_options_from_dict(_mapping(data["resize"], "resize"))
            if "resize" in data
            else None
        ),
        max_per_message=_optional_number(data, "maxPerMessage"),
        max_per_request=_optional_number(data, "maxPerRequest"),
    )


def _image_limits_to_dict(limits: ModelImageInputLimits) -> dict[str, Any]:
    result = _present_numbers(
        {
            "maxPerMessage": limits.max_per_message,
            "maxPerRequest": limits.max_per_request,
        }
    )
    if limits.resize is not None:
        result["resize"] = _resize_options_to_dict(limits.resize)
    return result


def _resize_options_from_dict(data: Mapping[str, Any]) -> ModelImageResizeOptions:
    return ModelImageResizeOptions(
        max_width=_optional_number(data, "maxWidth"),
        max_height=_optional_number(data, "maxHeight"),
        max_bytes=_optional_number(data, "maxBytes"),
        jpeg_quality=_optional_number(data, "jpegQuality"),
    )


def _resize_options_to_dict(options: ModelImageResizeOptions) -> dict[str, Any]:
    return _present_numbers(
        {
            "maxWidth": options.max_width,
            "maxHeight": options.max_height,
            "maxBytes": options.max_bytes,
            "jpegQuality": options.jpeg_quality,
        }
    )


def _present_numbers(values: dict[str, float | None]) -> dict[str, float]:
    return {key: value for key, value in values.items() if value is not None}


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return value


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _mapping(data.get(key), key)


def _required_list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _required_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _required_number(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    return _number(value, key)


def _optional_number(data: Mapping[str, Any], key: str) -> float | None:
    if key not in data:
        return None
    return _number(data[key], key)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return float(value)


def _string_dict(value: Any, name: str) -> dict[str, str]:
    data = _mapping(value, name)
    if any(not isinstance(item, str) for item in data.values()):
        raise ValueError(f"{name} values must be strings")
    return dict(data)


def _json_object(value: Any, name: str) -> JSONObject:
    data = _mapping(value, name)
    return {key: _json_value(item, f"{name}.{key}") for key, item in data.items()}


def _json_value(value: Any, name: str) -> JSONValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{name}[]") for item in value]
    if isinstance(value, Mapping):
        data = _mapping(value, name)
        return {key: _json_value(item, f"{name}.{key}") for key, item in data.items()}
    raise ValueError(f"{name} must contain only JSON-compatible values")
