import typing
import os
import sys
import time
import json
import urllib.parse
import asyncio
import threading
import contextlib
import textwrap
import requests
import logging
import discord
import mcp.server.mcpserver

# Set up logging for service.

class LogExceptionHandler(logging.StreamHandler):
    class ExceptionFormatter(logging.Formatter):
        def formatException(self, ei):
            return "ERROR " + super().formatException(ei).replace("\n", " ")

    def __init__(self, stream=None, fmt=None):
        super().__init__(stream=stream)
        self.setFormatter(LogExceptionHandler.ExceptionFormatter(fmt))

logging.basicConfig(
    handlers=[LogExceptionHandler(stream=sys.stdout, fmt="%(asctime)s %(levelname)s: %(message)s")],
    level=os.environ.get("LOG_LEVEL", logging.INFO),
    force=True,
)

log = logging.getLogger()

# Initialize configuration.

service_name: str = os.environ.get("SERVICE_NAME", "feedback-discord")
service_role: str = os.environ.get("SERVICE_ROLE", "feedback")
service_port: int = int(os.environ.get("PORT", "8000"))
secret_key: str = os.environ.get("SECRET_KEY", "unknown")
channel_id: str = os.environ.get("CHANNEL_ID", "unknown")
robot_name: str = os.environ.get("ROBOT_NAME", "unknown")
endpoint_url: str = os.environ.get("ENDPOINT_URL", "")
messages_url: str = os.environ.get("MESSAGES_URL", "")
registry_url: str = os.environ.get("REGISTRY_URL", "")

# Create the mcp server.

mcp = mcp.server.mcpserver.MCPServer(service_name, log_level="WARNING")

class FeedbackSessionMap:
    """
    Maintains a tree of session-message-reply linkage.
    """

    def __init__(self, message_limit: int = 65536):
        self._session_map: dict[str, str] = {}
        self._block = threading.Lock()
        self._limit = message_limit

    def set(self, message_id: str, session_id: str) -> tuple[str, str]:
        with self._block:
            while len(self._session_map) > self._limit:
                del self._session_map[next(iter(self._session_map))]
            self._session_map[message_id] = session_id
        return session_id, message_id

    def add(self, message_id: str, reply_to: str) -> tuple[str, str]:
        session_id = message_id
        with self._block:
            if reply_to in self._session_map:
                session_id = self._session_map[reply_to]
            while len(self._session_map) > self._limit:
                del self._session_map[next(iter(self._session_map))]
            self._session_map[message_id] = session_id
        return session_id, message_id
    
mapping = FeedbackSessionMap(message_limit=65536)

discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

@client.event
async def on_message(message: discord.Message):
    if not message or repr(message.channel.id) != channel_id:
        return
    if message.author == client.user:
        return
    if not message.reference:
        log.info(f". appending message ✉️")
    else:
        log.info(f". capturing a reply 📧")
    session_id, message_id = mapping.add(
        message_id=repr(message.id),
        reply_to=repr(message.reference.message_id) if message.reference else "",
    )
    if messages_url:
        try:
            await asyncio.to_thread(
                requests.post,
                url=messages_url,
                json={
                    "session_id": session_id,
                    "message_id": message_id,
                    "content": message.content,
                },
                timeout=15,
            )
        except IOError:
            log.exception(". failed to notify agent")

@client.event
async def on_ready():
    log.info(f". connected as '{client.user.name.lower() if client.user else 'unknown'}'")

class ChannelPostResult(typing.TypedDict):
    message_id: str
    channel_id: str

class TextPart(typing.TypedDict):
    type: typing.Literal["text"]
    text: str

class IdeaPart(typing.TypedDict):
    type: typing.Literal["idea"]
    text: str

class FailPart(typing.TypedDict):
    type: typing.Literal["fail"]
    text: str

class ToolPart(typing.TypedDict):
    type: typing.Literal["tool"]
    name: str
    params: dict[str, object]

class Message(typing.TypedDict):
    parts: list[TextPart | IdeaPart | FailPart | ToolPart]

@contextlib.contextmanager
def api_error_wrapper():
    try:
        yield
    except requests.exceptions.HTTPError as eX:
        if eX.response:
            if eX.response.status_code == 400:
                raise RuntimeError(
                    "Server rejected the message. Check the markdown/content and message payload.",
                )
            if eX.response.status_code == 401:
                raise RuntimeError(
                    "Bot token is invalid or expired. Do not retry with same credentials.",
                )
            if eX.response.status_code == 403:
                raise RuntimeError(
                    "Bot does not have permission to post in this channel. Do not retry with same credentials.",
                )
            if eX.response.status_code == 404:
                raise RuntimeError(
                    "Channel was not found or is inaccessible. Verify the channel configuration.",
                )
            if eX.response.status_code == 429:
                raise RuntimeError(
                    "Request was rate-limited. Wait before retrying.",
                )
            if eX.response.status_code >= 500:
                raise RuntimeError(
                    f"Server returned http {eX.response.status_code}. Do not retry.",
                )
        raise RuntimeError(
            f"Server returned unexpected error.",
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Failed to respond within allotted time. Wait before retrying.",
        )
    except requests.exceptions.RequestException:
        raise RuntimeError(
            "Failed to communicate with server. Check network connectivity and try again.",
        )

@mcp.tool()
def post_to_channel(session_id: str, message: Message) -> ChannelPostResult:
    """
    Post a message of markdown parts to the configured Discord channel to
    notify channel participants or ask them a question.

    Args:
        session_id: Durable context correlation id for linking feedback.
        message: Envelope {parts: [...]} wrapping an ordered list of parts
            assembled into a single posted message, each rendered according
            to its type:
                {type: "text", text: str}: plain markdown, shown as-is
                {type: "idea", text: str}: wrapped in italics
                {type: "fail", text: str}: error wrapped in block
                {type: "tool", name: str, params: dict}: rendered as a
                    name(params) call inside a code block
            The message may contain a notification, question, context, and
            response options. Keep the complete interaction prompt within
            this single list of parts. Limit combined text body length to
            1024 characters.

    Returns:
        The message_id of the posted message with the target channel_id;
        together to be used as a correlation key for subsequent feedback
        from a user communicating in the configured channel.
        On exception, error reason will be raised as runtime error.
    """

    timeout_seconds = 15

    body = "..."

    if message.get("parts"):
        body = ""
        for part in message["parts"]:
            if part["type"] == "tool":
                content = f"```\n{part['name']}\n{json.dumps(part.get('params', {}), indent=2)}\n```"
            else:
                content = part.get("text").strip() if part.get("text") else ""
                if part["type"] == "idea":
                    content = "\n".join(f"> {line}" for line in f"*{content}*".split("\n"))
                if part["type"] == "fail":
                    content = f"> 🌋 {content}"
            body += ("\n\n" if body else "") + content

    with api_error_wrapper():
        response = requests.post(
            url=f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={
                "Authorization": f"Bot {secret_key}",
                "Content-Type": "application/json",
            },
            json={
                "content": f"🤖 **{robot_name}**:\n\n{body}\n",
            },
            timeout=timeout_seconds,
        )

        response.raise_for_status()
        result = response.json()

        mapping.set(
            message_id=result["id"],
            session_id=session_id
        )

        return {
            "message_id": result["id"],
            "channel_id": channel_id,
        }

@mcp.tool()
def mark_as_queuing(session_id: str, message_id: str):
    """
    React to a posted message with reaction to indicate the agent is
    working on a response, so channel participants know their message
    or reply was received and is being processed.

    Args:
        session_id: Durable context correlation id (unused, but always
            supplied by the caller alongside every tool invocation).
        message_id: The id of the message to react to, as returned by
            post_to_channel or captured from an incoming message.

    On exception, error reason will be raised as a runtime error.
    """

    timeout_seconds = 15

    with api_error_wrapper():
        response = requests.put(
            url=f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{urllib.parse.quote('🕶️')}/@me",
            headers={
                "Authorization": f"Bot {secret_key}",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()

@mcp.tool()
def mark_as_handled(session_id: str, message_id: str):
    """
    Remove the reaction previously added by mark_as_queuing, once a
    response to the message has been posted, to signal processing is
    done.

    Args:
        session_id: Durable context correlation id (unused, but always
            supplied by the caller alongside every tool invocation).
        message_id: The id of the message to clear the reaction from, as
            returned by post_to_channel or captured from an incoming
            message.

    On exception, error reason will be raised as a runtime error.
    """

    timeout_seconds = 15

    with api_error_wrapper():
        response = requests.delete(
            url=f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{urllib.parse.quote('🕶️')}/@me",
            headers={
                "Authorization": f"Bot {secret_key}",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()

@mcp.tool()
def ping() -> str:
    """
    Health check. Returns "ok" when the service is up; "down" when down.
    """

    return "ok" if client.is_ready() else "down"

def add_to_registry(name: str, role: str, endpoint: str, url: str, port: int):
    if not endpoint or not url:
        log.warning("~ endpoint or url not configured")
        return

    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        try:
            requests.get(url=f"http://localhost:{port}", timeout=2)
            break
        except requests.exceptions.RequestException:
            time.sleep(1)
    else:
        log.warning("~ gave up waiting, continuing with registering")

    time.sleep(1)

    try:
        requests.post(
            url=url,
            json={
                "name": name,
                "role": role,
                "endpoint": endpoint,
            },
            timeout=15,
        )
    except IOError:
        log.exception(". failed to add to registry")

if __name__ == "__main__":
    print(textwrap.dedent(r"""
    .                                           /                                                     
    .           _/_                      o     //)         / /          /        /o                  /
    .  __,  , , /  __ _ _ _   __,  _ _  ,  _, /// _  _  __/ /  __,  _, /<     __/,  (   _, __ _   __/ 
    . (_/(_(_/_(__(_)/ / / /_(_/(_/ / /_(_(__///_(/_(/_(_/_/_)(_/(_(__/ |_---(_/_(_/_)_(__(_)/ (_(_/_ 
    .                                        /)                                                       
    .                                       (/                                                            
    . 
    . powered by automanic 🍣
    . """))

    log.info(f". filtering on discord channel {channel_id} [{secret_key[0:3]}...]")
    log.info(f". posting as '{robot_name}'")
    log.info(f". available tools:")
    for tool in mcp._tool_manager.list_tools():
        log.info(f". {tool.name}")
    gateway = threading.Thread(
        target=lambda: client.run(
            token=secret_key,
            log_level=logging.WARNING,
        ),
        name="service_gateway",
        daemon=True,
    )
    gateway.start()
    tooling = threading.Thread(
        target=lambda: add_to_registry(
            name=service_name,
            role=service_role,
            endpoint=endpoint_url,
            url=registry_url,
            port=service_port,
        ),
        name="service_tooling",
        daemon=True
    )
    tooling.start()
    try:
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=service_port,
            stateless_http=True,
            json_response=True,
        )
    finally:
        log.info(". shutting down 👋")
        if client.loop:
            asyncio.run_coroutine_threadsafe(
                coro=client.close(),
                loop=client.loop
            ).result(timeout=10)
        tooling.join(timeout=10)
        gateway.join(timeout=10)
