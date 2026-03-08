# server.py
import os
import json
import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

from tools import health_query, health_interpret, health_report, running_recommend

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONTEXT_DIR = BASE_DIR / "context"

def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
    
# ---- Load context once at startup (as in the design doc) ----
CONCEPTS = _load_json(CONTEXT_DIR / "domain_concepts.json")
USER     = _load_json(CONTEXT_DIR / "user_profile.json")

DEFAULT_DB_PATH = os.path.join("data", "running.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH)

app = Server("running-health-mcp")


@app.list_tools()
async def list_tools():
    return [
        Tool(**health_query.TOOL_DEF),
        Tool(**health_interpret.TOOL_DEF),
        Tool(**health_report.TOOL_DEF),
        Tool(**running_recommend.TOOL_DEF),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    ctx = {"concepts": CONCEPTS, "user": USER, "db": DB_PATH}

    if name == "health_query":
        result = await health_query.run(arguments, ctx)
    elif name == "health_interpret":
        result = await health_interpret.run(arguments, ctx)
    elif name == "health_report":
        result = await health_report.run(arguments, ctx)
    elif name == "running_recommend":
        result = await running_recommend.run(arguments, ctx)
    else:
        raise ValueError(f"Unknown tool: {name}")

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
