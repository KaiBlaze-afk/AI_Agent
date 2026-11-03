from typing import Callable, Dict, Any

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def register_tool(name: str, description: str, schema_example: str, default_enabled: bool = True):
    def decorator(func: Callable):
        TOOL_REGISTRY[name] = {
            "name": name,
            "description": description,
            "schema_example": schema_example,
            "default_enabled": default_enabled,
            "func": func,
        }
        return func
    return decorator


def get_registered_tools() -> list[dict]:
    return [
        {
            "name": k,
            "description": v["description"],
            "default_enabled": v["default_enabled"],
        }
        for k, v in TOOL_REGISTRY.items()
    ]


def format_tools_for_prompt(enabled_names: list[str]) -> str:
    lines = []
    for name in enabled_names:
        if name in TOOL_REGISTRY:
            t = TOOL_REGISTRY[name]
            lines.append(f"- {name}: {t['description']} | JSON format: {t['schema_example']}")
    return "\n".join(lines) if lines else "No tools enabled."


def execute_tool(name: str, payload: dict, context: dict) -> str:
    if name not in TOOL_REGISTRY:
        return f"Error: Tool '{name}' does not exist."
    try:
        func = TOOL_REGISTRY[name]["func"]
        return str(func(payload, context))
    except Exception as e:
        return f"Tool '{name}' execution error: {str(e)}"


from . import web_tools      # noqa: E402, F401
from . import system_tools   # noqa: E402, F401
from . import media_tools    # noqa: E402, F401