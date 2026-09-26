"""
Community Agent — LangGraph state graph.

Flow (matches the buildathon's required shape):
  User Request -> Agent Reasoning -> Tool Selection -> Swytchcode API
                -> Analyze Result -> Next Action -> Final Response

Implementation: a ReAct-style loop. Groq is given the Swytchcode tool
definitions; at each turn it either calls a tool or produces the final
answer. Every step (reasoning, tool chosen, tool result) is appended to
`state["trace"]` so the FastAPI layer can stream it to the demo UI.
"""

import os
import json
from typing import TypedDict, Annotated
import operator

from groq import Groq
from langgraph.graph import StateGraph, END
from dotenv import load_dotenv


from communityagent.agent.tools import TOOL_REGISTRY

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = 6

SYSTEM_PROMPT = """You are the Community Agent for a Discord/Telegram/X-style community.
Your job: monitor community conversations, understand what members need, and take
useful action using the tools available to you.

You have tools backed by Swytchcode:
- telegram_get_recent_messages / telegram_send_reply: read and respond to community chat
- slack_get_recent_messages / slack_post_message: read channel messages and notify the internal team
- notion_create_page: log discussions, issues, or knowledge for the team
- notion_query_database: check the shared Notion database for messages still marked
  "new" or that need follow-up because the status/urgency changed outside the agent
- notion_update_page: update the Notion record to mark it reviewed, escalated, or handled
- notion_query_database / notion_update_page: check Notion for entries with status
    'new' or high urgency that haven't been escalated yet, and mark them as handled once
    you've acted on them.
- resend_send_email: send an email digest or escalation

Reason step by step about the request. Decide which tool(s) are actually needed —
do not call a tool that isn't relevant. Use the RESULT of one tool call to inform
your next decision (e.g. only escalate via Slack/email if the issue is genuinely
high-priority). Check Notion for existing entries that still need action before
writing duplicate work or missing follow-up. When you have completed the task, give
a short final summary of what you did and why, with no further tool calls."""

TOOLS = [
    {
        "name": "telegram_get_recent_messages",
        "description": "Fetch recent messages from the monitored Telegram community chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Optional chat id override"},
                "limit": {"type": "integer", "description": "How many messages to fetch"},
            },
        },
    },
    {
        "name": "telegram_send_reply",
        "description": "Send a reply to a member in the Telegram community chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["chat_id", "text"],
        },
    },
    {
        "name": "slack_get_recent_messages",
        "description": "Fetch recent messages from a Slack channel for community monitoring or follow-up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Slack channel ID; defaults to SLACK_CHANNEL"},
                "limit": {"type": "integer", "description": "Maximum number of recent messages to fetch"},
            },
        },
    },
    {
        "name": "slack_post_message",
        "description": "Post a notification to the internal team Slack channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "channel": {"type": "string", "description": "Optional channel override"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "notion_create_page",
        "description": "Log a community message or issue into Notion with state fields like status and urgency so it can be reviewed later.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "database_id": {"type": "string"},
                "status": {"type": "string", "enum": ["new", "reviewed", "escalated"]},
                "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "notion_query_database",
        "description": "Query the Notion database for logged messages, optionally filtered by status (e.g. 'new'). Use this to check for unprocessed or updated entries before deciding on further action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data_source_id": {"type": "string"},
                "filter_status": {"type": "string", "description": "e.g. 'new', 'reviewed', 'escalated'"},
            },
        },
    },
    {
        "name": "notion_update_page",
        "description": "Update a Notion entry's Status and/or Urgency after the agent has acted on it (e.g. mark as 'reviewed' or 'escalated').",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "status": {"type": "string"},
                "urgency": {"type": "string"},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "resend_send_email",
        "description": "Send an email digest or escalation notice to the community lead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "to_email": {"type": "string"},
            },
            "required": ["subject", "body"],
        },
    },
]


GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }
    for tool in TOOLS
]


class AgentState(TypedDict):
    user_request: str
    messages: Annotated[list, operator.add]  # Groq chat-completions history
    trace: Annotated[list, operator.add]      # step-by-step log for the demo UI
    steps: int
    final_response: str
    done: bool


def agent_reasoning_node(state: AgentState) -> dict:
    """Call Groq with the current message history; it decides the next move."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *state["messages"]],
        max_tokens=1024,
        tools=GROQ_TOOLS,
        tool_choice="auto",
    )

    assistant_message = response.choices[0].message
    new_messages = [assistant_message.model_dump(exclude_none=True)]
    trace_entries = []

    if assistant_message.content:
        trace_entries.append({"type": "reasoning", "content": assistant_message.content})

    tool_calls = assistant_message.tool_calls or []
    for tool_call in tool_calls:
        trace_entries.append({
            "type": "tool_selected",
            "tool": tool_call.function.name,
            "input": json.loads(tool_call.function.arguments or "{}"),
        })

    done = not tool_calls
    final_response = assistant_message.content or "" if done else state.get("final_response", "")

    return {
        "messages": new_messages,
        "trace": trace_entries,
        "steps": state["steps"] + 1,
        "done": done,
        "final_response": final_response,
    }


def tool_execution_node(state: AgentState) -> dict:
    """Execute every function call from the last Groq response."""
    last_msg = state["messages"][-1]
    tool_calls = last_msg.get("tool_calls", [])

    tool_result_messages = []
    trace_entries = []

    for tool_call in tool_calls:
        function = tool_call["function"]
        tool_name = function["name"]
        fn = TOOL_REGISTRY.get(tool_name)
        tool_inputs = json.loads(function.get("arguments") or "{}")
        result = fn(**tool_inputs) if fn else {"ok": False, "error": "unknown tool"}

        trace_entries.append({
            "type": "tool_result",
            "tool": tool_name,
            "result": result,
        })

        tool_result_messages.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "content": json.dumps(result),
        })

    return {"messages": tool_result_messages, "trace": trace_entries}


def should_continue(state: AgentState) -> str:
    if state["done"] or state["steps"] >= MAX_STEPS:
        return "end"
    return "continue"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_reasoning_node)
    graph.add_node("tools", tool_execution_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent", should_continue, {"continue": "tools", "end": END}
    )
    graph.add_edge("tools", "agent")

    return graph.compile()


def run_agent(user_request: str) -> dict:
    """Run the compiled graph once for a given user request. Returns final state."""
    app = build_graph()
    initial_state: AgentState = {
        "user_request": user_request,
        "messages": [{"role": "user", "content": user_request}],
        "trace": [{"type": "user_request", "content": user_request}],
        "steps": 0,
        "final_response": "",
        "done": False,
    }
    result = app.invoke(initial_state)
    return {
        "final_response": result["final_response"],
        "trace": result["trace"],
    }
