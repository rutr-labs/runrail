from typing import Any

from jinja2 import Environment, StrictUndefined

_jinja = Environment(undefined=StrictUndefined, autoescape=False)


def render(value: str, context: dict[str, Any]) -> str:
    return _jinja.from_string(value).render(**context)


def render_mapping(values: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {key: render(value, context) if isinstance(value, str) else value
            for key, value in values.items()}

