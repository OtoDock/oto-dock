"""A tool call's arguments checked against the schema the tool was listed with.

The MCP SDK's own check (``@server.call_tool()`` validates by default) answers
the first jsonschema error without naming the argument, and it refuses a value
that misses an enum only by its case. A server registers its handler with
``@server.call_tool(validate_input=False)`` and asks this module instead:

    TOOL_ARGS = ToolArgs()

    @server.list_tools()
    async def list_tools():
        return TOOL_ARGS.listed([...])

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        arguments, refusal = TOOL_ARGS.check(name, arguments)
        if refusal is not None:
            return refusal
        ...

A string that equals exactly one member of its enum once both are folded
(case, and ``-`` or a space read as ``_``) is taken as that member. Whatever
is still invalid is refused the way the SDK refused it, with every problem
named by its argument and an enum's allowed values listed, so the model can
call again with them.

Every custom MCP that uses it carries a byte copy named ``_tool_args.py`` (an
MCP imports nothing outside its own folder): edit this file, then copy it.
"""

import json

import jsonschema
from mcp import types

# The schema keywords the fold walks, and the validator a refused value
# failed (named once: copies of this file must not repeat a literal).
_ENUM = "enum"
_PROPERTIES = "properties"
_ITEMS = "items"
_MAX_PROBLEMS = 5


def _fold(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _enum_member(value, members):
    """The one member of a string enum ``value`` folds onto, else ``value``."""
    if not isinstance(value, str) or value in members:
        return value
    if not all(isinstance(m, str) for m in members):
        return value
    folded = _fold(value)
    matches = [m for m in members if _fold(m) == folded]
    return matches[0] if len(matches) == 1 else value


def _normalized(value, schema):
    """``value`` with its near-miss enum strings replaced, walking an object's
    properties and an array's items. The unchanged parts are the caller's own
    objects; a changed path is a new container, so nothing is mutated."""
    if not isinstance(schema, dict):
        return value
    members = schema.get(_ENUM)
    if isinstance(members, list):
        return _enum_member(value, members)
    props = schema.get(_PROPERTIES)
    if isinstance(value, dict) and isinstance(props, dict):
        out = value
        for key, sub in props.items():
            if key in value:
                new = _normalized(value[key], sub)
                if new is not value[key]:
                    if out is value:
                        out = dict(value)
                    out[key] = new
        return out
    items = schema.get(_ITEMS)
    if isinstance(value, list) and isinstance(items, dict):
        new_items = [_normalized(v, items) for v in value]
        if any(n is not o for n, o in zip(new_items, value, strict=True)):
            return new_items
    return value


def _where(error) -> str:
    out = ""
    for part in error.absolute_path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _problem(error) -> str:
    where = _where(error)
    if error.validator == _ENUM:
        allowed = ", ".join(json.dumps(v, default=str) for v in error.validator_value)
        return f"{where or 'the value'} must be one of {allowed} (got {json.dumps(error.instance, default=str)})"
    return f"{where}: {error.message}" if where else error.message


def _refusal(name: str, problems: list[str]) -> types.CallToolResult:
    text = (f"Invalid arguments for {name}: " + "; ".join(problems)
            + f". Call {name} again with corrected arguments.")
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


class ToolArgs:
    """The schemas one server's tools were listed with, and the check."""

    def __init__(self) -> None:
        self._schemas: dict = {}

    def listed(self, tools: list) -> list:
        """Record what ``list_tools`` returns, replacing the record (the SDK
        lists again on a cache miss before a call, so it stays the schema
        the model was shown), and return it unchanged."""
        self._schemas = {getattr(t, "name", None): getattr(t, "inputSchema", None) for t in tools}
        return tools

    def check(self, name: str, arguments) -> tuple[dict, types.CallToolResult | None]:
        """(the arguments to go on with, None), or (them, the refusal). A
        tool that was never listed passes unchecked, as with the SDK. A
        schema jsonschema cannot read raises, as the SDK's check did."""
        if not isinstance(arguments, dict):
            arguments = {}
        schema = self._schemas.get(name)
        if not isinstance(schema, dict):
            return arguments, None
        arguments = _normalized(arguments, schema)
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        errors = list(cls(schema).iter_errors(arguments))
        if not errors:
            return arguments, None
        # The SDK reported best_match; it leads, the rest follow by relevance.
        first = jsonschema.exceptions.best_match(errors)
        rest = sorted((e for e in errors if e is not first), key=jsonschema.exceptions.relevance, reverse=True)
        problems = [_problem(e) for e in [first, *rest][:_MAX_PROBLEMS]]
        return arguments, _refusal(name, problems)
