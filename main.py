"""
Banking MCP Server — shared by all three agents.
Each agent authenticates with its own API key (X-Agent-Key header).
Tools are namespaced per agent: googleadk_*, langchain_*, langgraph_*
All tools read/write from a shared Supabase project.
"""

import sys, pathlib
# DNS patch: must be imported before any library that opens sockets
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import dns_patch  # noqa: F401

import os
import json
import asyncio
from typing import Any, AsyncGenerator
from datetime import datetime

from uuid import uuid4
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sse_starlette.sse import EventSourceResponse
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Banking MCP Server", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

AGENT_KEYS: dict[str, str] = {
    os.environ.get("GOOGLEADK_AGENT_KEY", "googleadk-secret-key"): "googleadk",
    os.environ.get("LANGCHAIN_AGENT_KEY", "langchain-secret-key"): "langchain",
    os.environ.get("LANGGRAPH_AGENT_KEY", "langgraph-secret-key"): "langgraph",
}

db: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_agent_id(request: Request) -> str:
    key = request.headers.get("X-Agent-Key", "")
    agent = AGENT_KEYS.get(key)
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid agent key")
    return agent


# ---------------------------------------------------------------------------
# Tool implementations (shared logic, called by namespace-specific handlers)
# ---------------------------------------------------------------------------

def _table(agent: str, name: str) -> str:
    """Return Supabase table name for this agent."""
    return f"{agent}_{name}"


def check_balance(db: Client, agent: str, account_id: str) -> dict:
    r = db.table(_table(agent, "accounts")).select("id,owner,balance,currency").eq("id", account_id).single().execute()
    if not r.data:
        return {"error": "Account not found"}
    return r.data


def transfer_funds(db: Client, agent: str, from_id: str, to_id: str, amount: float) -> dict:
    from_acc = db.table(_table(agent, "accounts")).select("balance").eq("id", from_id).single().execute()
    to_acc = db.table(_table(agent, "accounts")).select("balance").eq("id", to_id).single().execute()
    if not from_acc.data or not to_acc.data:
        return {"error": "Account not found"}
    if from_acc.data["balance"] < amount:
        return {"error": "Insufficient funds"}
    db.table(_table(agent, "accounts")).update({"balance": from_acc.data["balance"] - amount}).eq("id", from_id).execute()
    db.table(_table(agent, "accounts")).update({"balance": to_acc.data["balance"] + amount}).eq("id", to_id).execute()
    db.table(_table(agent, "transactions")).insert({
        "from_account": from_id, "to_account": to_id,
        "amount": amount, "type": "transfer", "created_at": datetime.utcnow().isoformat()
    }).execute()
    return {"status": "ok", "transferred": amount}


def transaction_history(db: Client, agent: str, account_id: str, limit: int = 10) -> dict:
    r = db.table(_table(agent, "transactions")).select("*").or_(
        f"from_account.eq.{account_id},to_account.eq.{account_id}"
    ).order("created_at", desc=True).limit(limit).execute()
    return {"transactions": r.data or []}


def pay_bill(db: Client, agent: str, account_id: str, biller: str, amount: float) -> dict:
    acc = db.table(_table(agent, "accounts")).select("balance").eq("id", account_id).single().execute()
    if not acc.data:
        return {"error": "Account not found"}
    if acc.data["balance"] < amount:
        return {"error": "Insufficient funds"}
    db.table(_table(agent, "accounts")).update({"balance": acc.data["balance"] - amount}).eq("id", account_id).execute()
    db.table(_table(agent, "transactions")).insert({
        "from_account": account_id, "to_account": None,
        "amount": amount, "type": "bill_payment", "biller": biller,
        "created_at": datetime.utcnow().isoformat()
    }).execute()
    return {"status": "ok", "paid_to": biller, "amount": amount}


def open_account(db: Client, agent: str, owner: str, currency: str = "USD") -> dict:
    import uuid
    account_id = str(uuid.uuid4())[:8].upper()
    db.table(_table(agent, "accounts")).insert({
        "id": account_id, "owner": owner, "balance": 0.0, "currency": currency,
        "created_at": datetime.utcnow().isoformat()
    }).execute()
    return {"status": "created", "account_id": account_id}


def close_account(db: Client, agent: str, account_id: str) -> dict:
    acc = db.table(_table(agent, "accounts")).select("balance").eq("id", account_id).single().execute()
    if not acc.data:
        return {"error": "Account not found"}
    if acc.data["balance"] > 0:
        return {"error": "Withdraw funds before closing"}
    db.table(_table(agent, "accounts")).delete().eq("id", account_id).execute()
    return {"status": "closed"}


def loan_eligibility(db: Client, agent: str, account_id: str, amount: float) -> dict:
    acc = db.table(_table(agent, "accounts")).select("balance").eq("id", account_id).single().execute()
    if not acc.data:
        return {"error": "Account not found"}
    balance = acc.data["balance"]
    eligible = balance >= amount * 0.2
    max_loan = balance * 5
    return {"eligible": eligible, "max_loan": round(max_loan, 2), "requested": amount}


CURRENCY_RATES = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "INR": 83.5, "JPY": 149.5}

def currency_conversion(amount: float, from_ccy: str, to_ccy: str) -> dict:
    f = CURRENCY_RATES.get(from_ccy.upper())
    t = CURRENCY_RATES.get(to_ccy.upper())
    if not f or not t:
        return {"error": "Unsupported currency"}
    converted = round(amount * (t / f), 4)
    return {"from": from_ccy, "to": to_ccy, "amount": amount, "converted": converted}


# ---------------------------------------------------------------------------
# Tool registry — namespaced by agent
# ---------------------------------------------------------------------------

TOOL_DEFS: dict[str, list[dict]] = {}
TOOL_HANDLERS: dict[str, dict] = {}  # agent -> {tool_name: fn}

def _build_tools(agent: str) -> None:
    prefix = f"{agent}_"
    tools = [
        {
            "name": f"{prefix}check_balance",
            "description": "Get current balance for an account",
            "inputSchema": {
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"]
            }
        },
        {
            "name": f"{prefix}transfer_funds",
            "description": "Transfer money between two accounts",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from_id": {"type": "string"},
                    "to_id": {"type": "string"},
                    "amount": {"type": "number"}
                },
                "required": ["from_id", "to_id", "amount"]
            }
        },
        {
            "name": f"{prefix}transaction_history",
            "description": "Get recent transactions for an account",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 10}
                },
                "required": ["account_id"]
            }
        },
        {
            "name": f"{prefix}pay_bill",
            "description": "Pay a bill from an account",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "biller": {"type": "string"},
                    "amount": {"type": "number"}
                },
                "required": ["account_id", "biller", "amount"]
            }
        },
        {
            "name": f"{prefix}open_account",
            "description": "Open a new bank account",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "currency": {"type": "string", "default": "USD"}
                },
                "required": ["owner"]
            }
        },
        {
            "name": f"{prefix}close_account",
            "description": "Close a bank account (must have zero balance)",
            "inputSchema": {
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"]
            }
        },
        {
            "name": f"{prefix}loan_eligibility",
            "description": "Check if account is eligible for a loan",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "amount": {"type": "number"}
                },
                "required": ["account_id", "amount"]
            }
        },
        {
            "name": f"{prefix}currency_conversion",
            "description": "Convert amount between currencies",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "from_ccy": {"type": "string"},
                    "to_ccy": {"type": "string"}
                },
                "required": ["amount", "from_ccy", "to_ccy"]
            }
        },
    ]
    TOOL_DEFS[agent] = tools

    def make_handler(fn, uses_db: bool, **fixed):
        def handler(args: dict):
            if uses_db:
                return fn(db, agent, **{k: args[k] for k in args})
            return fn(**{k: args[k] for k in args})
        return handler

    TOOL_HANDLERS[agent] = {
        f"{prefix}check_balance": lambda a: check_balance(db, agent, a["account_id"]),
        f"{prefix}transfer_funds": lambda a: transfer_funds(db, agent, a["from_id"], a["to_id"], a["amount"]),
        f"{prefix}transaction_history": lambda a: transaction_history(db, agent, a["account_id"], a.get("limit", 10)),
        f"{prefix}pay_bill": lambda a: pay_bill(db, agent, a["account_id"], a["biller"], a["amount"]),
        f"{prefix}open_account": lambda a: open_account(db, agent, a["owner"], a.get("currency", "USD")),
        f"{prefix}close_account": lambda a: close_account(db, agent, a["account_id"]),
        f"{prefix}loan_eligibility": lambda a: loan_eligibility(db, agent, a["account_id"], a["amount"]),
        f"{prefix}currency_conversion": lambda a: currency_conversion(a["amount"], a["from_ccy"], a["to_ccy"]),
    }


for _agent in ["googleadk", "langchain", "langgraph"]:
    _build_tools(_agent)


# ---------------------------------------------------------------------------
# MCP SSE endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ── SSE session queue store ──────────────────────────────────────────────────
_sse_sessions: dict[str, asyncio.Queue] = {}


@app.get("/mcp/sse")
async def mcp_sse(request: Request, agent: str = Depends(get_agent_id)):
    """Standard MCP SSE endpoint.
    Emits the 'endpoint' event first, then streams responses back to client.
    """
    session_id = uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue

    async def event_stream() -> AsyncGenerator[dict, None]:
        try:
            # 1. Official MCP protocol requirement: send endpoint URI first
            yield {
                "event": "endpoint",
                "data": f"/mcp/messages?session_id={session_id}"
            }
            # 2. Stream message events queued for this session
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {
                        "event": "message",
                        "data": json.dumps(msg) if isinstance(msg, dict) else str(msg)
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            _sse_sessions.pop(session_id, None)

    return EventSourceResponse(event_stream())


@app.post("/mcp/messages")
async def mcp_messages(request: Request, session_id: str = "", agent: str = Depends(get_agent_id)):
    """Receives JSON-RPC messages from MCP client (SSE transport)."""
    body = await request.json()
    msg_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})

    queue = _sse_sessions.get(session_id)

    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"banking-mcp-{agent}", "version": "1.0.0"}
            }
        }
    elif method == "notifications/initialized":
        return Response(status_code=202)
    elif method == "tools/list":
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOL_DEFS.get(agent, [])}
        }
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = TOOL_HANDLERS.get(agent, {}).get(tool_name)
        if not handler:
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Tool '{tool_name}' not found for agent '{agent}'"}
            }
        else:
            try:
                result = handler(arguments)
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                        "isError": False
                    }
                }
            except Exception as e:
                resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": str(e)}],
                        "isError": True
                    }
                }
    else:
        resp = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method '{method}' not supported"}
        }

    if queue:
        await queue.put(resp)
    return Response(status_code=202)


@app.post("/mcp/initialize")
async def mcp_initialize(request: Request, agent: str = Depends(get_agent_id)):
    body = await request.json()
    return {
        "jsonrpc": "2.0",
        "id": body.get("id"),
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"banking-mcp-{agent}", "version": "1.0.0"}
        }
    }


@app.post("/mcp/tools/list")
async def mcp_tools_list(request: Request, agent: str = Depends(get_agent_id)):
    body = await request.json()
    return {
        "jsonrpc": "2.0",
        "id": body.get("id"),
        "result": {"tools": TOOL_DEFS[agent]}
    }


@app.post("/mcp/tools/call")
async def mcp_tools_call(request: Request, agent: str = Depends(get_agent_id)):
    body = await request.json()
    params = body.get("params", {})
    tool_name: str = params.get("name", "")
    arguments: dict = params.get("arguments", {})

    # Enforce namespace — agent can only call its own tools
    if not tool_name.startswith(f"{agent}_"):
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32601, "message": f"Tool '{tool_name}' not available for agent '{agent}'"}
        }

    handler = TOOL_HANDLERS.get(agent, {}).get(tool_name)
    if not handler:
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "error": {"code": -32601, "message": "Tool not found"}
        }

    try:
        result = handler(arguments)
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False
            }
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "content": [{"type": "text", "text": str(e)}],
                "isError": True
            }
        }
