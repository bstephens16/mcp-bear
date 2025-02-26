#  server.py
#
#  Copyright (c) 2025 Junpei Kawamoto
#
#  This software is released under the MIT License.
#
#  http://opensource.org/licenses/mit-license.php
import asyncio
import json
import logging
import webbrowser
from asyncio import Queue, Future, QueueEmpty
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from typing import cast, AsyncIterator
from urllib.parse import urlencode, quote, unquote_plus

from fastapi import FastAPI, Request
from mcp.server import FastMCP
from mcp.server.fastmcp import Context
from pydantic import Field
from starlette.datastructures import QueryParams
from uvicorn import Config, Server
from uvicorn.config import LOGGING_CONFIG

BASE_URL = "bear://x-callback-url"


@dataclass
class ErrorResponse(Exception):
    errorCode: int
    errorMessage: str

    def __str__(self) -> str:
        return self.errorMessage


def register_callback(api: FastAPI, path: str) -> Queue[Future[QueryParams]]:
    queue = Queue[Future[QueryParams]]()

    @api.get(f"/{path}/success", status_code=HTTPStatus.NO_CONTENT, include_in_schema=False)
    def success(request: Request) -> None:
        try:
            future = queue.get_nowait()
            future.set_result(request.query_params)
        except QueueEmpty:
            pass

    @api.get(f"/{path}/error", status_code=HTTPStatus.NO_CONTENT, include_in_schema=False)
    def error(request: Request) -> None:
        try:
            future = queue.get_nowait()

            q = request.query_params
            future.set_exception(
                ErrorResponse(
                    errorCode=int(q.get("error-Code") or "0"),
                    errorMessage=q.get("errorMessage") or "",
                )
            )
        except QueueEmpty:
            pass

    return queue


@dataclass
class AppContext:
    open_note_results: Queue[Future[QueryParams]]
    create_results: Queue[Future[QueryParams]]
    tags_results: Queue[Future[QueryParams]]
    open_tag_results: Queue[Future[QueryParams]]
    todo_results: Queue[Future[QueryParams]]
    today_results: Queue[Future[QueryParams]]
    search_results: Queue[Future[QueryParams]]
    grab_url_results: Queue[Future[QueryParams]]


def create_server(token: str, callback_host: str, callback_port: int) -> FastMCP:
    logger = logging.getLogger(__name__)

    @asynccontextmanager
    async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
        callback = FastAPI()

        log_config = deepcopy(LOGGING_CONFIG)
        log_config["handlers"]["access"]["stream"] = "ext://sys.stderr"
        server = Server(
            Config(
                app=callback,
                host=callback_host,
                port=callback_port,
                log_level="warning",
                log_config=log_config,
            )
        )

        logger.info(f"Starting callback server on {callback_host}:{callback_port}")
        server_task = asyncio.create_task(server.serve())
        try:
            yield AppContext(
                open_note_results=register_callback(callback, "open-note"),
                create_results=register_callback(callback, "create"),
                tags_results=register_callback(callback, "tags"),
                open_tag_results=register_callback(callback, "open-tag"),
                todo_results=register_callback(callback, "todo"),
                today_results=register_callback(callback, "today"),
                search_results=register_callback(callback, "search"),
                grab_url_results=register_callback(callback, "grab-url"),
            )
        finally:
            logger.info("Stopping callback server")
            server.should_exit = True
            await server_task

    mcp = FastMCP("Bear", lifespan=app_lifespan)

    @mcp.tool()
    async def open_note(
        ctx: Context,
        id: str | None = Field(description="note unique identifier", default=None),
        title: str | None = Field(description="note title", default=None),
    ) -> str:
        """Open a note identified by its title or id and return its content."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.open_note_results.put(future)

        params = {
            "new_window": "no",
            "float": "no",
            "show_window": "no",
            "open_note": "no",
            "selected": "no",
            "pin": "no",
            "edit": "no",
            "x-success": f"http://{callback_host}:{callback_port}/open-note/success",
            "x-error": f"http://{callback_host}:{callback_port}/open-note/error",
        }
        if id is not None:
            params["id"] = id
        if title is not None:
            params["title"] = title

        webbrowser.open(f"{BASE_URL}/open-note?{urlencode(params, quote_via=quote)}")
        res = await future

        return unquote_plus(res.get("note") or "")

    @mcp.tool()
    async def create(
        ctx: Context,
        title: str | None = Field(description="note title", default=None),
        text: str | None = Field(description="note body", default=None),
        tags: list[str] | None = Field(description="list of tags", default=None),
        timestamp: bool = Field(description="prepend the current date and time to the text", default=False),
    ) -> str:
        """Create a new note and return its unique identifier. Empty notes are not allowed."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.create_results.put(future)

        params = {
            "open_note": "no",
            "new_window": "no",
            "float": "no",
            "show_window": "no",
            "x-success": f"http://{callback_host}:{callback_port}/create/success",
            "x-error": f"http://{callback_host}:{callback_port}/create/error",
        }
        if title is not None:
            params["title"] = title
        if text is not None:
            params["text"] = text
        if tags is not None:
            params["tags"] = ",".join(tags)
        if timestamp:
            params["timestamp"] = "yes"

        webbrowser.open(f"{BASE_URL}/create?{urlencode(params, quote_via=quote)}")
        res = await future

        return res.get("identifier") or ""

    @mcp.tool()
    async def tags(
        ctx: Context,
    ) -> list[str]:
        """Return all the tags currently displayed in Bear’s sidebar."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.tags_results.put(future)

        params = {
            "token": token,
            "x-success": f"http://{callback_host}:{callback_port}/tags/success",
            "x-error": f"http://{callback_host}:{callback_port}/tags/error",
        }

        webbrowser.open(f"{BASE_URL}/tags?{urlencode(params, quote_via=quote)}")
        res = await future

        notes = cast(list[dict], json.loads(res.get("tags") or "[]"))
        return [note["name"] for note in notes if "name" in note]

    @mcp.tool()
    async def open_tag(
        ctx: Context,
        name: str = Field(description="tag name or a list of tags divided by comma"),
    ) -> list[str]:
        """Show all the notes which have a selected tag in bear."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.open_tag_results.put(future)

        params = {
            "name": name,
            "token": token,
            "x-success": f"http://{callback_host}:{callback_port}/open-tag/success",
            "x-error": f"http://{callback_host}:{callback_port}/open-tag/error",
        }

        webbrowser.open(f"{BASE_URL}/open-tag?{urlencode(params, quote_via=quote)}")
        res = await future

        notes = cast(list[dict], json.loads(res.get("notes") or "[]"))
        return [f"{note.get('title')} (ID: {note.get('identifier')})" for note in notes]

    @mcp.tool()
    async def todo(
        ctx: Context,
        search: str | None = Field(description="string to search", default=None),
    ) -> list[str]:
        """Select the Todo sidebar item."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.todo_results.put(future)

        params = {
            "show_window": "no",
            "token": token,
            "x-success": f"http://{callback_host}:{callback_port}/todo/success",
            "x-error": f"http://{callback_host}:{callback_port}/todo/error",
        }
        if search is not None:
            params["search"] = search

        webbrowser.open(f"{BASE_URL}/todo?{urlencode(params, quote_via=quote)}")
        res = await future

        notes = cast(list[dict], json.loads(res.get("notes") or "[]"))
        return [f"{note.get('title')} (ID: {note.get('identifier')})" for note in notes]

    @mcp.tool()
    async def today(
        ctx: Context,
        search: str | None = Field(description="string to search", default=None),
    ) -> list[str]:
        """Select the Today sidebar item."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.today_results.put(future)

        params = {
            "show_window": "no",
            "token": token,
            "x-success": f"http://{callback_host}:{callback_port}/today/success",
            "x-error": f"http://{callback_host}:{callback_port}/today/error",
        }
        if search is not None:
            params["search"] = search

        webbrowser.open(f"{BASE_URL}/today?{urlencode(params, quote_via=quote)}")
        res = await future

        notes = cast(list[dict], json.loads(res.get("notes") or "[]"))
        return [f"{note.get('title')} (ID: {note.get('identifier')})" for note in notes]

    @mcp.tool()
    async def search(
        ctx: Context,
        term: str | None = Field(description="string to search", default=None),
        tag: str | None = Field(description="tag to search into", default=None),
    ) -> list[str]:
        """Show search results in Bear for all notes or for a specific tag."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.search_results.put(future)

        params = {
            "show_window": "no",
            "token": token,
            "x-success": f"http://{callback_host}:{callback_port}/search/success",
            "x-error": f"http://{callback_host}:{callback_port}/search/error",
        }
        if term is not None:
            params["term"] = term
        if tag is not None:
            params["tag"] = tag

        webbrowser.open(f"{BASE_URL}/search?{urlencode(params, quote_via=quote)}")
        res = await future

        notes = cast(list[dict], json.loads(res.get("notes") or "[]"))
        return [f"{note.get('title')} (ID: {note.get('identifier')})" for note in notes]

    @mcp.tool()
    async def grab_url(
        ctx: Context,
        url: str = Field(description="url to grab"),
        tags: list[str] | None = Field(
            description="list of tags. If tags are specified in the Bear’s web content preferences, this parameter is ignored.",
            default=None,
        ),
    ) -> str:
        """Create a new note with the content of a web page and return its unique identifier."""
        app_ctx: AppContext = ctx.request_context.lifespan_context
        future = Future[QueryParams]()
        await app_ctx.grab_url_results.put(future)

        params = {
            "url": url,
            "x-success": f"http://{callback_host}:{callback_port}/grab-url/success",
            "x-error": f"http://{callback_host}:{callback_port}/grab-url/error",
        }
        if tags is not None:
            params["tags"] = ",".join(tags)

        webbrowser.open(f"{BASE_URL}/grab-url?{urlencode(params, quote_via=quote)}")
        res = await future

        return res.get("identifier") or ""

    return mcp
