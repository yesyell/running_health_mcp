# server.py
import os
import json
import asyncio
from importlib import import_module

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONTEXT_DIR = BASE_DIR / "context"
TOOL_MODULES = {
    "health_query": "tools.health_query",
    "health_interpret": "tools.health_interpret",
    "health_report": "tools.health_report",
    "running_recommend": "tools.running_recommend",
}

def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
    
# ---- Load context once at startup (as in the design doc) ----
CONCEPTS = _load_json(CONTEXT_DIR / "domain_concepts.json")
USER     = _load_json(CONTEXT_DIR / "user_profile.json")

DEFAULT_DB_PATH = os.path.join("data", "running.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

app = Server("running-health-mcp")


def _load_tool_module(name: str):
    module_path = TOOL_MODULES.get(name)
    if not module_path:
        raise ValueError(f"Unknown tool: {name}")
    return import_module(module_path)


@app.list_tools()
async def list_tools():
    tools = []
    for name in TOOL_MODULES:
        try:
            module = _load_tool_module(name)
        except Exception:
            continue
        tools.append(Tool(**module.TOOL_DEF))
    return tools

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    ctx = {"concepts": CONCEPTS, "user": USER, "db": DB_PATH}

    try:
        module = _load_tool_module(name)
    except Exception as e:
        result = {
            "error": f"tool_load_failed: {type(e).__name__}: {e}",
            "tool": name,
        }
    else:
        result = await module.run(arguments, ctx)

    # ✅ Claude가 기대하는 tool result 형태로 감싸기
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, default=str),
            }
        ]
    }


async def main():
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
