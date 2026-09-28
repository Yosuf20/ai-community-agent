"""
Swytchcode tool wrappers — CLI-based.

Swytchcode works as a CLI, not a REST API you call directly:
    1. `npx swytchcode`                       -> install/bootstrap the CLI
    2. `swytchcode get <service>`              -> pull that service's manifest
                                                   (schema + available actions)
    3. authenticate once per service           -> Swytchcode stores/refreshes
                                                   the real credentials for you
    4. `swytchcode exec <service>.<action> --key value ...` -> actually call it

So instead of building HTTP requests ourselves, every tool function below just
shells out to `swytchcode exec ...` and parses whatever it prints back.

IMPORTANT: The exact action names (e.g. "notion.create_page") and their flag
names are PLACEHOLDERS marked with # TODO. Confirm the real ones by running:
    swytchcode get notion
    swytchcode get slack
    swytchcode get telegram
    swytchcode get resend
and reading the manifest each one prints (it lists the available actions and
their expected arguments) — then adjust the `action` string and `**kwargs`
passed to `swytchcode_exec` in each function below. Nothing else needs to change.

Every function still returns a plain dict, so agent/graph.py (which reasons
over these results) doesn't need any changes at all.
"""

import os
import json
import shlex
import subprocess
import tempfile
from dotenv import load_dotenv

load_dotenv()

# If `swytchcode` isn't on your PATH after the initial `npx swytchcode` setup,
# set SWYTCHCODE_CLI="npx swytchcode" in your .env instead.
SWYTCHCODE_CLI = os.getenv("SWYTCHCODE_CLI", "swytchcode")
CLI_TIMEOUT_SECONDS = 20


def swytchcode_exec(action: str, body: dict | None = None, **inputs) -> dict:
    """
    Run `swytchcode exec <action> [--body <tmpfile>] --input key=value ... --json`.
    Use `body=` for nested JSON payloads, **inputs for flat path/header/query params.
    """
    cmd = shlex.split(SWYTCHCODE_CLI) + ["exec", action]
    tmp_path = None

    if body is not None:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(body, tmp)
        tmp.close()
        tmp_path = tmp.name
        cmd.extend(["--body", tmp_path])

    for key, value in inputs.items():
        if value is None or value == "":
            continue
        cmd.extend(["--input", f"{key}={value}"])

    try:
        cmd.append("--json")
        if os.name == "nt":
            cmd = ["cmd.exe", "/d", "/c", *cmd]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"swytchcode exec timed out: {' '.join(cmd)}"}
    except FileNotFoundError:
        return {"ok": False, "error": "swytchcode CLI not found on PATH."}
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    stdout, stderr = result.stdout.strip(), result.stderr.strip()
    if result.returncode != 0:
        try:
            return {"ok": False, "error": json.loads(stderr).get("error", stderr)}
        except json.JSONDecodeError:
            return {"ok": False, "error": stderr or f"exit code {result.returncode}"}

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": True, "data": stdout}

    status_code = parsed.get("status_code")
    if status_code is not None and status_code >= 400:
        return {"ok": False, "error": f"API error {status_code}: {parsed.get('data')}"}
    return {"ok": True, "data": parsed.get("data", parsed)}


# ---------------------------------------------------------------------------
# TELEGRAM — read/monitor community messages
# ---------------------------------------------------------------------------
def telegram_get_recent_messages(chat_id: str | None = None, limit: int = 10) -> dict:
    """Fetch recent messages from a monitored Telegram chat/channel."""
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    return swytchcode_exec(
        "telegram.get_messages",
        chat_id=chat_id,
        limit=limit,
    )


def telegram_send_reply(chat_id: str, text: str) -> dict:
    """Reply to a member in the monitored Telegram chat."""
    return swytchcode_exec(
        "telegram.send_message",
        chat_id=chat_id,
        text=text,
    )


# ---------------------------------------------------------------------------
# SLACK — notify the internal team
# ---------------------------------------------------------------------------
def slack_get_recent_messages(channel: str | None = None, limit: int = 10) -> dict:
    """Fetch recent messages from a Slack channel."""
    channel = channel or os.getenv("SLACK_CHANNEL", "")
    return swytchcode_exec(
        "slack.conversations.history.list",
        channel=channel,
        limit=limit,
    )


def slack_post_message(text: str, channel: str | None = None) -> dict:
    """Post a message/notification to the internal Slack channel."""
    channel = channel or os.getenv("SLACK_CHANNEL", "#community-updates")
    body = {"channel": channel, "text": text}
    return swytchcode_exec("slack.chat.postmessage.create", body=body)


# ---------------------------------------------------------------------------
# NOTION — log / organize community knowledge
# ---------------------------------------------------------------------------
def notion_create_page(
    title: str,
    content: str,
    database_id: str | None = None,
    status: str = "new",
    urgency: str = "low",
) -> dict:
    """Log a community discussion/issue as a new page in the Notion database."""
    database_id = database_id or os.getenv("NOTION_DATABASE_ID", "")
    body = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Message": {"rich_text": [{"text": {"content": content}}]},
            "Status": {"select": {"name": status}},
            "Urgency": {"select": {"name": urgency}},
        },
    }
    return swytchcode_exec("notion.page.create", body=body)


def notion_query_database(data_source_id: str | None = None, filter_status: str | None = None) -> dict:
    """Query the Notion database for entries, optionally filtered by Status."""
    data_source_id = data_source_id or os.getenv("NOTION_DATABASE_ID", "")
    body = {}
    if filter_status:
        body["filter"] = {"property": "Status", "select": {"equals": filter_status}}
    return swytchcode_exec("notion.query.create", body=body or None, data_source_id=data_source_id)


def notion_update_page(page_id: str, status: str | None = None, urgency: str | None = None) -> dict:
    """Update an existing Notion page's Status/Urgency after the agent acts on it."""
    properties = {}
    if status:
        properties["Status"] = {"select": {"name": status}}
    if urgency:
        properties["Urgency"] = {"select": {"name": urgency}}
    body = {"properties": properties} if properties else None
    return swytchcode_exec("notion.page.update", body=body, page_id=page_id)


# ---------------------------------------------------------------------------
# RESEND — email digests / escalations
# ---------------------------------------------------------------------------
def resend_send_email(subject: str, body: str, to_email: str | None = None) -> dict:
    """Send an email digest or escalation notice via Resend."""
    to_email = to_email or os.getenv("RESEND_TO_EMAIL", "")
    from_email = os.getenv("RESEND_FROM_EMAIL", "agent@yourdomain.dev")
    return swytchcode_exec(
        "resend.send_email",  # TODO: confirm real action name
        **{"from": from_email, "to": to_email, "subject": subject, "body": body},
    )


# Registry so the agent graph can look tools up by name for logging/demo display
TOOL_REGISTRY = {
    "telegram_get_recent_messages": telegram_get_recent_messages,
    "telegram_send_reply": telegram_send_reply,
    "slack_get_recent_messages": slack_get_recent_messages,
    "slack_post_message": slack_post_message,
    "notion_create_page": notion_create_page,
    "notion_query_database": notion_query_database,
    "notion_update_page": notion_update_page,
    "resend_send_email": resend_send_email,
}
