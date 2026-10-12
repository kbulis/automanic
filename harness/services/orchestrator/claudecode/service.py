import os
import sys
import uuid
import json
import asyncio
import threading
import dataclasses
import contextlib
import textwrap
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
    ...
    """

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
        self.model = agent_model

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

        session_id = str(uuid.uuid4())

        helping: list[Agent.ToolParam] = [Agent.ToolParam(
            name="help-request_feature",
            description="""
                When encountering the need for a tool interaction that seems
                unavailable, use this tool to request the feature with a very
                brief description of what you would like to accomplish and why.
            """,
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
        ), Agent.ToolParam(
            name="help-ping",
            description="""
                Health check. Returns "ok" when the service is up.
            """,
            input_schema={
                "type": "object",
                "properties": {
                },
            },
        )]

        def write_system_configuration():
            toolbox: list[Agent.Service] = []

            with self._block:
                toolbox = [
                    service
                    for service in self._toolbox.values()
                    if service.role != "feedback"
                ]

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
                        + "\n\n".join([service.direction.strip() for service in toolbox if service.direction.strip() != ""])
                        + "\n"
                )

            with open(os.path.join(os.getcwd(), ".mcp.json"), "w", encoding="utf-8") as file:
                json.dump({
                    "mcpServers": {
                        f"automanic-{service.name}": {
                            "type": "http",
                            "url": service.endpoint,
                        }
                        for service in toolbox
                    },
                }, file, indent=4)

            with open(os.path.join(os.getcwd(), ".settings.json"), "w", encoding="utf-8") as file:
                json.dump({
                    "enableAllProjectMcpServers": True,
                    "permissions": {
                        "allow": [
                            f"mcp__automanic-{service.name}__{tool.name}"
                            for service in toolbox for tool in service.tools
                        ],
                    },
                }, file, indent=4)

        try:
            write_system_configuration()
        except Exception:
            log.exception("! write system configuration failed")

        # BUGBUG need to invoke self._cli like _api but to call with --session-id
        # because starting new session... prompt to check services, but only after
        # saving system prompt and complete tooling + helping

        while True:
            stop_now = False
            messages = []

            with self._block:
                if not self._queue and not self._done:
                    self._block.wait()
                if not self._queue and self._done:
                    stop_now = True
                messages = self._queue
                self._queue = []

            if stop_now:
                return

            for message in messages:
                await self._post_to_tool(
                    tool_name="mark_as_queuing",
                    role="feedback",
                    arguments={
                        "message_id": message.message_id,
                    },
                    session_id=message.session_id,
                )

                looping = True

                while looping:
                    answers: list[dict] = []

                    try:
                        response = { "content": [], "stop_reason": ""} # BUGBUG invoke claude code as process and stream stdout and stderror

                        results = []

                        for block in response.get("content", []):
                            if block.type == "tool_use":
                                if not block.name.startswith("help-"):
                                    result = await self._post_to_tool(
                                        session_id=message.session_id,
                                        full_name=block.name,
                                        arguments=block.input,
                                    )
                                    results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": [{
                                            "type": "text",
                                            "text": c.text
                                        } for c in result.content if c.type == "text"],
                                        "is_error": result.is_error,
                                    })
                                else:
                                    results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": "ok",
                                    })
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

                        if response.get("stop_reason", "") == "end_turn":
                            looping = False

                        if not results:
                            looping = False

                    except Exception as eX: # BUGBUG need claude call specific erroring
                        error_message = str(eX) # BUGBUG pull out actual error messaging for user to see
                        answers.append({
                            "type": "fail",
                            "text": error_message,
                        })
                        looping = False

                    if answers:
                        await self._post_to_tool(
                            session_id=message.session_id,
                            role="feedback",
                            tool_name="post_to_channel",
                            arguments={
                                "message": {
                                    "parts": answers + [{
                                        "type": "idea",
                                        "text": f"(total = ...)", # BUGBUG include stats on token usage for calls
                                    }],
                                },
                            },
                        )

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
