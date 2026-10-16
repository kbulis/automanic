import typing
import os
import sys
import re
import uuid
import json
import asyncio
import threading
import dataclasses
import contextlib
import textwrap
import datetime
import dotenv
import pydantic
import mcp
import mcp.client.streamable_http
import fastapi
import uvicorn
import logging

dotenv.load_dotenv()

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
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    force=True,
)

log = logging.getLogger()

# Initialize configuration.

agent_auth_token = os.getenv("AGENT_AUTH_TOKEN")
agent_model = os.getenv("AGENT_MODEL") or "claude-opus-5-5"

class Agent:
    """
    Claude code agent that drains queued user messages on a worker thread,
    running each through a headless claude code session tied to its feedback
    session, with registered mcp service tools reached through the local
    shim, posting results back via the feedback service.
    """

    @dataclasses.dataclass
    class SystemParam:
        text: str

    @dataclasses.dataclass
    class ToolParam:
        name: str
        description: str
        input_schema: dict[str, object]

    @dataclasses.dataclass
    class QueuedMessage:
        session_id: str
        message_id: str
        content: str

    @dataclasses.dataclass(kw_only=True)
    class BaseBlock:
        type: typing.Literal["tool_use", "thinking", "text", "fail"]

    @dataclasses.dataclass(kw_only=True)
    class ToolBlock(BaseBlock):
        type: typing.Literal["tool_use"] = "tool_use"
        id: str
        name: str
        input: dict[str, object]

    @dataclasses.dataclass(kw_only=True)
    class IdeaBlock(BaseBlock):
        type: typing.Literal["thinking"] = "thinking"
        thinking: str

    @dataclasses.dataclass(kw_only=True)
    class TextBlock(BaseBlock):
        type: typing.Literal["text"] = "text"
        text: str

    @dataclasses.dataclass(kw_only=True)
    class FailBlock(BaseBlock):
        type: typing.Literal["fail"] = "fail"
        text: str

    @dataclasses.dataclass
    class Context:
        history: list["Agent.ToolBlock | Agent.IdeaBlock | Agent.TextBlock | Agent.FailBlock"]
        input_tokens: int
        write_tokens: int
        started_on: datetime.datetime

    @dataclasses.dataclass
    class Response:
        role: typing.Literal["assistant"]
        content: list["Agent.ToolBlock | Agent.IdeaBlock | Agent.TextBlock | Agent.FailBlock"]
        input_tokens: int
        output_tokens: int
        num_turns: int

        @property
        def blocks(self) -> typing.Iterator["Agent.ToolBlock | Agent.IdeaBlock | Agent.TextBlock | Agent.FailBlock"]:
            for block in self.content:
                if block.type == "tool_use":
                    yield Agent.ToolBlock(
                        id=block.id,
                        name=block.name,
                        input=block.input,
                    )
                if block.type == "thinking":
                    yield Agent.IdeaBlock(
                        thinking=block.thinking,
                    )
                if block.type == "text":
                    yield Agent.TextBlock(
                        text=block.text,
                    )
                if block.type == "fail":
                    yield Agent.FailBlock(
                        text=block.text,
                    )

    @dataclasses.dataclass
    class Service:
        name: str
        role: str
        direction: str
        endpoint: str
        tools: list["Agent.ToolParam"]

    def __init__(self, agent_auth_token: str, agent_model: str):
        self._toolbox: dict[str, Agent.Service] = {}
        self._queue: list[Agent.QueuedMessage] = []
        self._block: threading.Condition = threading.Condition()
        self._done = False
        self._auth_token = agent_auth_token
        self.model = agent_model

    @property
    def toolbox(self) -> typing.Iterator[tuple[str, str, list["Agent.ToolParam"]]]:
        with self._block:
            for service in self._toolbox.values():
                yield service.name, service.role, service.tools

    @property
    def done(self) -> bool:
        with self._block:
            return self._done

    async def _post_to_tool(self, *, session_id: str, full_name: str = "", role: str = "", tool_name: str = "", arguments: dict[str, object]) -> mcp.types.CallToolResult:
        """
        Open a short-lived mcp session against the given service url and
        invoke a single tool call.
        """

        name = ""
        url = ""

        with self._block:
            for service in self._toolbox.values():
                if full_name and not full_name.startswith(service.name + "-"):
                    continue
                if not full_name and role and service.role != role:
                    continue
                for tool in service.tools:
                    if full_name and full_name == f"{service.name}-{tool.name}":
                        name = tool.name
                        url = service.endpoint
                        break
                    if not full_name and role == service.role and tool_name == tool.name:
                        name = tool.name
                        url = service.endpoint
                        break
                if name and url:
                    break

        if not name or not url:
            missing = f"{full_name}" or f"{role}:{tool_name}"
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(type="text", text=f"tool '{missing}' not found"),
                ],
                is_error=True,
            )

        try:
            async with mcp.client.streamable_http.streamable_http_client(url=url) as (read, write):
                async with mcp.ClientSession(read_stream=read, write_stream=write) as service:
                    await service.initialize()
                    return await service.call_tool(
                        name=name,
                        arguments={
                            **arguments,
                            "session_id": session_id,
                        },
                    )
        except Exception as eX:
            cause: BaseException = eX
            while isinstance(cause, BaseExceptionGroup) and cause.exceptions:
                cause = cause.exceptions[0]
            return mcp.types.CallToolResult(
                is_error=True,
                content=[
                    mcp.types.TextContent(type="text", text=f"tool '{name}' failed: {cause}"),
                ],
            )

    async def _invoke_agent(self, session_id: str, content: str, agent_timeout: float = 3600) -> Response:
        """
        Run claude code headless on the given content, starting or resuming
        the claude session tied to the given session, and wrap the streamed
        blocks in a response. Tool errors and claude code failures are kept
        as fail blocks alongside any work already streamed.
        """

        context_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{session_id}"))
        resuming = os.path.exists(os.path.join(
            os.getenv("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude"),
            "projects",
            re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()),
            context_id + ".jsonl",
        ))

        process = await asyncio.create_subprocess_exec(
            "claude",
            "--model", self.model,
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--mcp-config", os.path.join(os.getcwd(), ".mcp.json"),
            "--disallowedTools", "Bash,Task",
            "--allowedTools", "mcp__automanic",
            "--strict-mcp-config",
            "--resume" if resuming else "--session-id", context_id,
            "--",
            content,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
            env={
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
                "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": "1",
                **os.environ,
                "CLAUDE_CODE_OAUTH_TOKEN": self._auth_token,
                "ORCHESTRATION_SESSION_ID": session_id,
            },
            limit=16 * 1024 * 1024,
        )

        blocks: list[Agent.ToolBlock | Agent.IdeaBlock | Agent.TextBlock | Agent.FailBlock] = []
        result: dict = {}
        stderr = ""
        ootime = False
        errors: asyncio.Task | None = None

        try:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("claude code started without output pipes")

            async with asyncio.timeout(agent_timeout):
                errors = asyncio.create_task(process.stderr.read())

                async for line in process.stdout:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except Exception:
                        continue
                    if event.get("type") == "assistant":
                        message_content = event.get("message", {}).get("content")
                        for block in message_content if isinstance(message_content, list) else []:
                            if block.get("type") == "tool_use":
                                blocks.append(Agent.ToolBlock(
                                    id=block["id"],
                                    name=block["name"].removeprefix("mcp__automanic__"),
                                    input=block.get("input") or {},
                                ))
                            if block.get("type") == "thinking" and block.get("thinking"):
                                blocks.append(Agent.IdeaBlock(
                                    thinking=block["thinking"],
                                ))
                            if block.get("type") == "text" and block.get("text"):
                                blocks.append(Agent.TextBlock(
                                    text=block["text"],
                                ))
                    if event.get("type") == "user":
                        message_content = event.get("message", {}).get("content")
                        for block in message_content if isinstance(message_content, list) else []:
                            if block.get("type") != "tool_result" or not block.get("is_error"):
                                continue
                            error = block.get("content")
                            if isinstance(error, list):
                                error = " ".join(part.get("text", "") for part in error if part.get("type") == "text")
                            for i, match in enumerate(blocks):
                                if match.type == "tool_use" and match.id == block.get("tool_use_id", ""):
                                    blocks.insert(i + 1, Agent.FailBlock(
                                        text=f"{match.name} failed: {error or 'no details'}",
                                    ))
                                    break
                            else:
                                blocks.append(Agent.FailBlock(
                                    text=f"tool failed: {error or 'no details'}",
                                ))
                    if event.get("type") == "result":
                        result = event

                stderr = (await errors).decode("utf-8", errors="replace").strip()

                await process.wait()
        except TimeoutError:
            ootime = True
        finally:
            if errors and not errors.done():
                errors.cancel()
            if process.returncode is None:
                    process.kill()
                    await process.wait()

        if ootime and not result:
            blocks.append(Agent.FailBlock(
                text=f"claude code timed out after {agent_timeout:g} seconds",
            ))
        elif not result:
            blocks.append(Agent.FailBlock(
                text=stderr or f"claude code exited with code {process.returncode} and no result",
            ))
        elif result.get("is_error"):
            blocks.append(Agent.FailBlock(
                text=result.get("result") or result.get("subtype") or "claude code failed",
            ))

        return self.Response(
            role="assistant",
            content=blocks,
            output_tokens=result.get("usage", {}).get("output_tokens", 0),
            input_tokens=result.get("usage", {}).get("input_tokens", 0),
            num_turns=result.get("num_turns", 0),
        )

    def install_service(self, name: str, role: str, direction: str, endpoint: str, tools: list[ToolParam]):
        """
        Called from a handler thread to register an active service of tools
        for use in subsequent agent interactions.
        """

        if not name or not endpoint or not tools:
            return
        with self._block:
            self._toolbox[name] = self.Service(
                name=name,
                role=role,
                direction=direction,
                endpoint=endpoint,
                tools=tools,
            )

    def queue_message(self, session_id: str, message_id: str, content: str):
        """
        Called from a handler thread to enqueue a message for processing.
        """

        with self._block:
            self._queue.append(self.QueuedMessage(
                session_id=session_id,
                message_id=message_id,
                content=content,
            ))
            self._block.notify()
        
    def stop(self):
        """
        Signals to exit once all messages already queued are drained.
        """

        with self._block:
            self._done = True
            self._block.notify()

    async def run(self):
        """
        Loop forever, blocking until a message is queued or stop has
        been called. Drains and processes whatever is queued each pass,
        then exits once stopping and the queue is empty.
        """

        while True:
            current_toolbox: list[Agent.Service] = []
            stop_now = False
            messages = []

            with self._block:
                if not self._queue and not self._done:
                    self._block.wait()
                if not self._queue and self._done:
                    stop_now = True
                for service in self._toolbox.values():
                    if service.role == "feedback":
                        continue
                    current_toolbox.append(service)
                messages = self._queue
                self._queue = []

            if stop_now:
                return

            try:
                with open(os.path.join(os.getcwd(), "CLAUDE.md"), "w", encoding="utf-8") as file:
                    file.write(
                        textwrap.dedent("""
                            You are a long-lived, general-purpose agent that can research,
                            write code, run commands, and use connected tools to complete
                            the user's task end to end. When you respond, brevity is very
                            valuable; be brief and concise.
                            When you check tools for connectivity and status, note that
                            tooling will be logically grouped by prefix (tools prefixed
                            the same should be considered a group). If a group has a ping
                            tool, use that to assess status and connectivity.
                            If you find that you need tools to accomplish a request, but
                            are unable to identify one, use help-request_feature.
                            Format agent responses as markdown (sans tables).

                            Orchestration policy playbook:
                        """).strip()
                            + "\n\n"
                            + "\n\n".join([
                                service.direction.strip()
                                for service in current_toolbox
                                if service.direction.strip() != "" and service.role != "feedback"
                            ])
                            + "\n"
                    )
            except Exception:
                log.exception(f"! unable to record latest system prompt")

            for message in messages:
                try:
                    await self._post_to_tool(
                        tool_name="mark_as_queuing",
                        role="feedback",
                        arguments={
                            "message_id": message.message_id,
                        },
                        session_id=message.session_id,
                    )

                    answers: list[dict] = []

                    try:
                        response = await self._invoke_agent(
                            session_id=message.session_id,
                            content=message.content,
                        )

                        for block in response.blocks:
                            if block.type == "tool_use":
                                answers.append({
                                    "type": "tool",
                                    "name": block.name,
                                    "params": block.input,
                                })
                            if block.type == "thinking":
                                answers.append({
                                    "type": "idea",
                                    "text": block.thinking,
                                })
                            if block.type == "text":
                                answers.append({
                                    "type": "text",
                                    "text": block.text,
                                })
                            if block.type == "fail":
                                answers.append({
                                    "type": "fail",
                                    "text": block.text,
                                })

                        answers.append({
                            "type": "idea",
                            "text": f"(total = {response.input_tokens}/{response.output_tokens}, depth = {response.num_turns})",
                        })

                    except RuntimeError as eX:
                        answers.append({
                            "type": "fail",
                            "text": str(eX) or type(eX).__name__,
                        })

                    if answers:
                        await self._post_to_tool(
                            session_id=message.session_id,
                            role="feedback",
                            tool_name="post_to_channel",
                            arguments={
                                "message": {
                                    "parts": answers,
                                },
                            },
                        )

                except Exception as eX:
                    log.exception(f"! message '{message.message_id}' handling failed")
                    await self._post_to_tool(
                        session_id=message.session_id,
                        role="feedback",
                        tool_name="post_to_channel",
                        arguments={
                            "message": {
                                "parts": [{
                                    "type": "fail",
                                    "text": str(eX) or type(eX).__name__,
                                }],
                            },
                        },
                    )

                finally:
                    await self._post_to_tool(
                        tool_name="mark_as_handled",
                        role="feedback",
                        arguments={
                            "message_id": message.message_id,
                        },
                        session_id=message.session_id,
                    )

@contextlib.asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    log.info(". orchestrator spinning up...")

    if not agent_auth_token:
        raise RuntimeError("agent auth token is not set")

    app.state.agent = Agent(agent_auth_token=agent_auth_token, agent_model=agent_model)

    with open(os.path.join(os.getcwd(), ".mcp.json"), "w", encoding="utf-8") as file:
        json.dump({
            "mcpServers": {
                "automanic": {
                    "type": "http",
                    "url": f"http://127.0.0.1:{os.getenv('PORT', '8000')}/mcp",
                    "headers": {
                        "x-session-id": "${ORCHESTRATION_SESSION_ID:-}",
                    },
                },
            },
        }, file, indent=4)

    worker = threading.Thread(
        target=lambda: asyncio.run(app.state.agent.run()),
        name="agent-worker",
        daemon=True,
    )
    worker.start()

    log.info(f". orchestrator started using model '{app.state.agent.model}'")

    yield

    app.state.agent.stop()
    worker.join(timeout=9)

    log.info(". orchestrator stopped")

app = fastapi.FastAPI(
    title="Orchestrator",
    description="Long-lived harness and mcp service registry",
    version="0.7.0",
    lifespan=lifespan,
)

class ServiceDescriptor(pydantic.BaseModel):
    name: str
    role: str
    direction: str
    endpoint: str

@app.post("/registry")
async def register_mcp_service(request: fastapi.Request, body: ServiceDescriptor):
    """
    Register an mcp service with the orchestrator.
    """

    agent: Agent = request.app.state.agent

    try:
        async with mcp.client.streamable_http.streamable_http_client(url=body.endpoint) as (read, write):
            async with mcp.ClientSession(read_stream=read, write_stream=write) as service:
                await service.initialize()
                agent.install_service(
                    name=body.name,
                    role=body.role,
                    direction=body.direction,
                    endpoint=body.endpoint,
                    tools=[
                        Agent.ToolParam(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.input_schema,
                        )
                        for tool in (await service.list_tools()).tools
                    ],
                )
    except Exception:
        log.exception("! install service failed")
        raise fastapi.HTTPException(
            status_code=500,
            detail="failure to install service",
        )

    return {
        "status": "registered",
    }

class PostedMessage(pydantic.BaseModel):
    session_id: str
    message_id: str
    content: str

@app.post("/messages")
async def receive_message(request: fastapi.Request, body: PostedMessage):
    """
    Accept a user message and queue for handling by agent.
    """

    agent: Agent = request.app.state.agent

    try:
        agent.queue_message(
            session_id=body.session_id,
            message_id=body.message_id,
            content=body.content,
        )
    except Exception:
        log.exception("! queue message failed")
        raise fastapi.HTTPException(
            status_code=500,
            detail="failure to queue message",
        )

    return {
        "status": "queued",
    }

@app.get("/health")
async def health():
    return {
        "status": "ok",
    }

@app.post("/mcp")
async def proxy(request: fastapi.Request):
    """
    Serve the localhost mcp shim that all agent tool calls pass through,
    answering the handshake and routing tool list and call requests.
    """

    agent: Agent = request.app.state.agent

    if not request.client or request.client.host not in ("127.0.0.1", "::1"):
        raise fastapi.HTTPException(
            status_code=403,
            detail="mcp proxy is localhost only",
        )

    body = await request.json()

    if "id" not in body:
        return fastapi.Response(status_code=202)

    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        result = mcp.types.InitializeResult(
            protocol_version=params.get("protocolVersion", ""),
            capabilities=mcp.types.ServerCapabilities(tools=mcp.types.ToolsCapability()),
            server_info=mcp.types.Implementation(name="orchestrator", version=request.app.version),
        )
    elif method == "ping":
        result = mcp.types.EmptyResult()
    elif method == "tools/list":
        result = mcp.types.ListToolsResult(
            tools=[
                mcp.types.Tool(
                    name=f"{name}-{tool.name}",
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for name, role, tools in agent.toolbox
                for tool in tools
                if role != "feedback"
            ] + [mcp.types.Tool(
                name="help-request_feature",
                description=textwrap.dedent("""
                    When encountering the need for a tool interaction that seems
                    unavailable, use this tool to request the feature with a very
                    brief description of what you would like to accomplish and why.
                """).strip(),
                input_schema={
                    "type": "object",
                    "properties": {
                        "what": {
                            "type": "string",
                        },
                        "why": {
                            "type": "string",
                        },
                    },
                    "required": [
                        "what",
                        "why",
                    ]
                },
            )],
        )
    elif method == "tools/call":
        session_id = request.headers.get("x-session-id", "")
        tool_name = params.get("name", "")
        if tool_name not in ["help-request_feature"] + [
            f"{name}-{tool.name}"
            for name, role, tools in agent.toolbox
            for tool in tools
            if role != "feedback"
        ]:
            result = mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(type="text", text=f"tool '{tool_name}' not valid or not configured"),
                ],
                is_error=True,
            )
        elif not session_id:
            result = mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(type="text", text=f"tool '{tool_name}' called without a session id"),
                ],
                is_error=True,
            )
        else:
            arguments = params.get("arguments") or {}
            if tool_name == "help-request_feature":
                result = mcp.types.CallToolResult(
                    content=[
                        mcp.types.TextContent(type="text", text="ok"),
                    ],
                )
            else:
                result = await agent._post_to_tool(
                    session_id=session_id,
                    full_name=tool_name,
                    arguments=arguments,
                )
        if result.is_error:
            log.error(f"! tool '{tool_name}' returned error: " + " ".join(
                c.text for c in result.content if c.type == "text"
            ))
    else:
        return {
            "jsonrpc": "2.0",
            "id": body["id"],
            "error": {
                "code": -32601,
                "message": f"method '{method}' not found",
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": body["id"],
        "result": result.model_dump(by_alias=True, exclude_none=True, mode="json"),
    }

if __name__ == "__main__":
    print(textwrap.dedent(r"""
    .                                           /                                                 _                            
    .           _/_                      o     /          /         _/_         _/_              //            /            /  
    .  __,  , , /  __ _ _ _   __,  _ _  ,  _, /__ _   _, /_  _  (   /  _   __,  /  __ _      _, // __,  , , __/ _  _, __ __/ _ 
    . (_/(_(_/_(__(_)/ / / /_(_/(_/ / /_(_(__/(_)/ (_(__/ /_(/_/_)_(__/ (_(_/(_(__(_)/ (_---(__(/_(_/(_(_/_(_/_(/_(__(_)(_/_(/_
    .                                                                                                                          
    . 
    . 
    . powered by automanic 🍣
    . """))

    log.info(f". orchestrating")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level=logging.WARNING,
    )
