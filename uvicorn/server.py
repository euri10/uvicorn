import asyncio
import functools
import logging
import os
import platform
import signal
import socket
import sys
import threading
import time
from email.utils import formatdate
from multiprocessing.synchronize import Event as MEvent
from typing import Any, Optional

import click

from uvicorn._impl import Shutdown, check_multiprocess_shutdown_event, raise_shutdown

HANDLED_SIGNALS = (
    signal.SIGINT,  # Unix signal 2. Sent by Ctrl+C.
    signal.SIGTERM,  # Unix signal 15. Sent by `kill <pid>`.
)

logger = logging.getLogger("uvicorn.error")


class ServerState:
    """
    Shared servers state that is available between all protocol instances.
    """

    def __init__(self):
        self.total_requests = 0
        self.connections = set()
        self.tasks = set()
        self.default_headers = []


class Server:
    def __init__(
        self,
        config,
        shutdown_event: Optional[MEvent] = None,
    ):
        self.config = config
        self.shutdown_event = shutdown_event
        self.shutdown_trigger = None
        self.server_state = ServerState()
        self.tasks = []
        self.counter = 0
        self.started = False
        self.should_exit = False
        self.force_exit = False
        self.last_notified = 0

    def run(self, sockets=None, *args, **kwargs):
        logger.debug(f"run args:{args} kwargs:{kwargs}")
        if self.shutdown_event is not None:
            logger.debug(f"setting multiprocess trigger using : {self.shutdown_event}")
            self.shutdown_trigger = functools.partial(
                check_multiprocess_shutdown_event, self.shutdown_event, asyncio.sleep
            )
        self.config.setup_event_loop()
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.serve(sockets=sockets))

    async def serve(self, sockets=None):
        self.process_id = os.getpid()
        config = self.config
        if not config.loaded:
            config.load()

        self.lifespan = config.lifespan_class(config)

        self.install_signal_handlers()

        message = "Started server process [%d]"
        color_message = "Started server process [" + click.style("%d", fg="cyan") + "]"
        logger.info(message, self.process_id, extra={"color_message": color_message})

        await self.startup(sockets=sockets)
        await self.main_loop()
        await self.shutdown(sockets=sockets)

        message = "Finished server process [%d]"
        color_message = "Finished server process [" + click.style("%d", fg="cyan") + "]"
        logger.info(
            "Finished server process [%d]",
            self.process_id,
            extra={"color_message": color_message},
        )

    async def startup(self, sockets=None):
        await self.lifespan.startup()
        if self.lifespan.should_exit:
            self.should_exit = True
            return

        config = self.config

        create_protocol = functools.partial(
            config.http_protocol_class, config=config, server_state=self.server_state
        )

        loop = asyncio.get_event_loop()

        if sockets is not None:
            # Explicitly passed a list of open sockets.
            # We use this when the server is run from a Gunicorn worker.

            def _share_socket(sock: socket) -> socket:
                # Windows requires the socket be explicitly shared across
                # multiple workers (processes).
                from socket import fromshare  # type: ignore

                sock_data = sock.share(os.getpid())  # type: ignore
                return fromshare(sock_data)

            self.servers = []
            for sock in sockets:
                if config.workers > 1 and platform.system() == "Windows":
                    sock = _share_socket(sock)
                server = await loop.create_server(
                    create_protocol, sock=sock, ssl=config.ssl, backlog=config.backlog
                )
                self.servers.append(server)

        elif config.fd is not None:
            # Use an existing socket, from a file descriptor.
            sock = socket.fromfd(config.fd, socket.AF_UNIX, socket.SOCK_STREAM)
            server = await loop.create_server(
                create_protocol, sock=sock, ssl=config.ssl, backlog=config.backlog
            )
            message = "Uvicorn running on socket %s (Press CTRL+C to quit)"
            logger.info(message % str(sock.getsockname()))
            self.servers = [server]

        elif config.uds is not None:
            # Create a socket using UNIX domain socket.
            uds_perms = 0o666
            if os.path.exists(config.uds):
                uds_perms = os.stat(config.uds).st_mode
            server = await loop.create_unix_server(
                create_protocol, path=config.uds, ssl=config.ssl, backlog=config.backlog
            )
            os.chmod(config.uds, uds_perms)
            message = "Uvicorn running on unix socket %s (Press CTRL+C to quit)"
            logger.info(message % config.uds)
            self.servers = [server]

        else:
            # Standard case. Create a socket from a host/port pair.
            addr_format = "%s://%s:%d"
            if config.host and ":" in config.host:
                # It's an IPv6 address.
                addr_format = "%s://[%s]:%d"

            try:
                server = await loop.create_server(
                    create_protocol,
                    host=config.host,
                    port=config.port,
                    ssl=config.ssl,
                    backlog=config.backlog,
                )
            except OSError as exc:
                logger.error(exc)
                await self.lifespan.shutdown()
                sys.exit(1)
            port = config.port
            if port == 0:
                port = server.sockets[0].getsockname()[1]
            protocol_name = "https" if config.ssl else "http"
            message = f"Uvicorn running on {addr_format} (Press CTRL+C to quit)"
            color_message = (
                "Uvicorn running on "
                + click.style(addr_format, bold=True)
                + " (Press CTRL+C to quit)"
            )
            logger.info(
                message,
                protocol_name,
                config.host,
                port,
                extra={"color_message": color_message},
            )
            self.servers = [server]
        self.tasks.append(loop.create_task(raise_shutdown(self.shutdown_trigger)))
        self.tasks.append(loop.create_task(self.loop_tick()))
        self.tasks.extend(server.serve_forever() for server in self.servers)
        self.started = True

    async def main_loop(self):
        try:
            gathered_tasks = asyncio.gather(*self.tasks)
            await gathered_tasks
        except (Shutdown, KeyboardInterrupt) as e:
            logger.debug(f"raised shutdown exc: {e}")

    async def loop_tick(self):
        counter = 0
        should_exit = await self.on_tick(counter)
        while not should_exit:
            counter += 1
            await asyncio.sleep(0.1)
            should_exit = await self.on_tick(counter)

    async def on_tick(self, counter) -> bool:
        # Update the default headers, once per second.
        if counter % 10 == 0:
            current_time = time.time()
            current_date = formatdate(current_time, usegmt=True).encode()
            self.server_state.default_headers = [
                (b"date", current_date)
            ] + self.config.encoded_headers

            # Callback to `callback_notify` once every `timeout_notify` seconds.
            if self.config.callback_notify is not None:
                if current_time - self.last_notified > self.config.timeout_notify:
                    self.last_notified = current_time
                    await self.config.callback_notify()

        # Determine if we should exit.
        if self.should_exit:
            return True
        if self.config.limit_max_requests is not None:
            return self.server_state.total_requests >= self.config.limit_max_requests
        return False

    async def shutdown(self, sockets=None):
        logger.info("Shutting down")

        # Stop accepting new connections.
        for server in self.servers:
            server.close()
        for sock in sockets or []:
            sock.close()
        for server in self.servers:
            await server.wait_closed()

        # Request shutdown on all existing connections.
        for connection in list(self.server_state.connections):
            connection.shutdown()
        await asyncio.sleep(0.1)

        # Wait for existing connections to finish sending responses.
        if self.server_state.connections and not self.force_exit:
            msg = "Waiting for connections to close. (CTRL+C to force quit)"
            logger.info(msg)
            while self.server_state.connections and not self.force_exit:
                await asyncio.sleep(0.1)

        # Wait for existing tasks to complete.
        if self.server_state.tasks and not self.force_exit:
            msg = "Waiting for background tasks to complete. (CTRL+C to force quit)"
            logger.info(msg)
            while self.server_state.tasks and not self.force_exit:
                await asyncio.sleep(0.1)

        # Send the lifespan shutdown event, and wait for application shutdown.
        if not self.force_exit:
            await self.lifespan.shutdown()

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            # Signals can only be listened to from the main thread.
            return

        loop = asyncio.get_event_loop()
        if self.shutdown_trigger is None:
            self.signal_event = asyncio.Event()

            def _signal_handler(*_: Any) -> None:  # noqa: N803
                logger.debug("Received signal")
                self.signal_event.set()

            for signal_name in {"SIGINT", "SIGTERM", "SIGBREAK"}:
                if hasattr(signal, signal_name):
                    try:
                        loop.add_signal_handler(
                            getattr(signal, signal_name), _signal_handler
                        )
                    except NotImplementedError:
                        # Add signal handler may not be implemented on Windows
                        signal.signal(getattr(signal, signal_name), _signal_handler)

            self.shutdown_trigger = self.signal_event.wait  # type: ignore
