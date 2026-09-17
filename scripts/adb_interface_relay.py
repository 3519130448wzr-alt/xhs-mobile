#!/usr/bin/env python3
"""Forward a loopback TCP port to an ADB endpoint using an explicit route mode.

This is useful on macOS when a VPN owns the default route but the cloud-phone
security group permits only the stable public address of another interface.
Use ``--interface system`` explicitly to follow the current system route instead;
a failed named-interface connection never falls back to the system route.
The relay carries raw TCP only; it neither reads nor stores ADB credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import logging
import os
import re
import selectors
import socket
import socketserver
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

LOGGER = logging.getLogger("adb-interface-relay")
DARWIN_IP_BOUND_IF = 25
DEFAULT_BUFFER_SIZE = 64 * 1024


class RelayError(RuntimeError):
    """An expected relay configuration or connection error."""


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """Runtime settings shared by all connection-handler threads."""

    interface: str
    interface_index: int | None
    remote_host: str
    remote_port: int
    connect_timeout: float
    buffer_size: int
    route_config: str | None = None


def _current_config(config: RelayConfig) -> RelayConfig:
    """Reload only the outbound route for each new TCP connection, fail closed.

    Existing connections keep their route. The controller explicitly disconnects
    its offline ADB transport before testing another route. This file cannot
    change the listener or destination and is never a source of shell commands.
    """
    if config.route_config is None:
        return config
    path = Path(config.route_config)
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise RelayError("route configuration must be a private user-owned file")
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise RelayError("route configuration is too large")
        document = json.loads(raw)
        interface = document["interface"]
        if set(document) != {"interface"} or not isinstance(interface, str):
            raise ValueError("invalid route document")
        if interface != "system" and not re.fullmatch(r"en[0-9]{1,3}", interface):
            raise ValueError("invalid physical interface")
        index = None if interface == "system" else socket.if_nametoindex(interface)
        return replace(config, interface=interface, interface_index=index)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RelayError("dynamic route is unavailable or invalid") from exc


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _loopback_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("listen address must be an IPv4 loopback address") from exc
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("listen address must be an IPv4 loopback address")
    return str(address)


def _bind_to_interface(outbound: socket.socket, config: RelayConfig) -> None:
    """Bind an outbound IPv4 socket to the configured network interface."""

    try:
        if sys.platform == "darwin":
            # IP_BOUND_IF is defined by Darwin but is not exposed by Python's
            # socket module. Its stable Darwin socket-option value is 25.
            outbound.setsockopt(
                socket.IPPROTO_IP,
                DARWIN_IP_BOUND_IF,
                config.interface_index,
            )
            return

        if sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE"):
            interface_name = config.interface.encode("utf-8") + b"\0"
            outbound.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface_name)
            return
    except OSError as exc:
        raise RelayError(
            f"cannot bind outbound socket to interface {config.interface!r}: {exc}"
        ) from exc

    raise RelayError(
        f"interface binding is unsupported on platform {sys.platform!r}; "
        "use macOS or Linux with SO_BINDTODEVICE support"
    )


def _connect_remote(config: RelayConfig) -> socket.socket:
    """Resolve and connect to the remote endpoint using the selected interface."""

    try:
        addresses = socket.getaddrinfo(
            config.remote_host,
            config.remote_port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise RelayError(
            f"cannot resolve IPv4 address for {config.remote_host!r}: {exc}"
        ) from exc

    errors: list[str] = []
    for family, socket_type, protocol, _canonical_name, address in addresses:
        outbound = socket.socket(family, socket_type, protocol)
        try:
            outbound.settimeout(config.connect_timeout)
            if config.interface != "system":
                _bind_to_interface(outbound, config)
            # ADB sends small request/response frames. Do not let Nagle's
            # coalescing add a delayed-ACK wait to these interactive exchanges.
            outbound.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            outbound.connect(address)
            outbound.settimeout(None)
            return outbound
        except (OSError, RelayError) as exc:
            errors.append(f"{address[0]}:{address[1]} ({exc})")
            outbound.close()

    detail = "; ".join(errors) if errors else "no IPv4 addresses returned"
    raise RelayError(
        f"cannot connect to {config.remote_host}:{config.remote_port} "
        f"through {config.interface!r}: {detail}"
    )


def _relay_bidirectionally(
    client: socket.socket,
    remote: socket.socket,
    *,
    buffer_size: int,
) -> None:
    """Relay independently bounded directions without blocking reverse traffic.

    Each destination has at most ``buffer_size`` queued bytes. Backpressure
    disables only the corresponding source read; the opposite direction stays
    selectable. An EOF propagates as a write half-close only after queued bytes
    have been sent, so a peer can still return a response after receiving EOF.
    """
    if buffer_size <= 0:
        raise ValueError("buffer_size must be positive")
    peers = {client: remote, remote: client}
    pending = {client: bytearray(), remote: bytearray()}
    reading = {client: True, remote: True}
    write_closed = {client: False, remote: False}
    selector = selectors.DefaultSelector()

    def refresh_events() -> None:
        for source, destination in peers.items():
            if not reading[source] and not pending[destination] and not write_closed[destination]:
                with contextlib.suppress(OSError):
                    destination.shutdown(socket.SHUT_WR)
                write_closed[destination] = True
        for endpoint, destination in peers.items():
            events = 0
            if reading[endpoint] and len(pending[destination]) < buffer_size:
                events |= selectors.EVENT_READ
            if pending[endpoint]:
                events |= selectors.EVENT_WRITE
            try:
                selector.get_key(endpoint)
            except KeyError:
                if events:
                    selector.register(endpoint, events)
            else:
                if events:
                    selector.modify(endpoint, events)
                else:
                    selector.unregister(endpoint)

    try:
        client.setblocking(False)
        remote.setblocking(False)
        refresh_events()
        while selector.get_map():
            for key, events in selector.select():
                source = cast(socket.socket, key.fileobj)
                destination = peers[source]
                if events & selectors.EVENT_WRITE and pending[source]:
                    try:
                        sent = source.send(pending[source])
                    except (BlockingIOError, InterruptedError):
                        pass
                    else:
                        if sent == 0:
                            raise ConnectionResetError("relay destination stopped accepting bytes")
                        del pending[source][:sent]
                if events & selectors.EVENT_READ and reading[source]:
                    available = buffer_size - len(pending[destination])
                    if available:
                        try:
                            chunk = source.recv(available)
                        except (BlockingIOError, InterruptedError):
                            pass
                        except ConnectionResetError:
                            reading[source] = False
                        else:
                            if chunk:
                                pending[destination].extend(chunk)
                            else:
                                reading[source] = False
            refresh_events()
    finally:
        selector.close()


class RelayRequestHandler(socketserver.BaseRequestHandler):
    """Serve one local ADB connection."""

    def handle(self) -> None:
        server = cast("RelayServer", self.server)
        client_host, client_port = self.client_address
        client_label = f"{client_host}:{client_port}"

        try:
            config = _current_config(server.config)
            remote = _connect_remote(config)
        except RelayError as exc:
            LOGGER.error("[%s] remote connection failed: %s", client_label, exc)
            return

        LOGGER.info(
            "[%s] connected to %s:%s through %s",
            client_label,
            config.remote_host,
            config.remote_port,
            config.interface,
        )
        try:
            with remote:
                # The accepted socket is TCP too; configure this direction
                # explicitly instead of relying on listener option inheritance.
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _relay_bidirectionally(
                    self.request,
                    remote,
                    buffer_size=config.buffer_size,
                )
        except OSError as exc:
            LOGGER.warning("[%s] relay stopped after a socket error: %s", client_label, exc)
        finally:
            LOGGER.info("[%s] disconnected", client_label)


class RelayServer(socketserver.ThreadingTCPServer):
    """Thread-per-connection TCP server carrying a shared relay configuration."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        server_address: tuple[str, int],
        config: RelayConfig,
    ) -> None:
        self.config = config
        loopback = _loopback_ipv4(server_address[0])
        super().__init__((loopback, server_address[1]), RelayRequestHandler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Forward a loopback TCP port to a remote ADB endpoint using a chosen "
            "network interface, or explicitly follow system routing."
        )
    )
    parser.add_argument(
        "--interface",
        required=True,
        help="outbound interface name, for example en0, or system to follow current OS routing",
    )
    parser.add_argument("--remote-host", required=True, help="remote ADB host or IPv4 address")
    parser.add_argument(
        "--route-config", help="private JSON file reloaded before each new outbound connection",
    )
    parser.add_argument("--remote-port", required=True, type=_port, help="remote ADB TCP port")
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        type=_loopback_ipv4,
        help="IPv4 loopback address to listen on (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--listen-port",
        default=6100,
        type=_port,
        help="local TCP port to listen on (default: 6100)",
    )
    parser.add_argument(
        "--connect-timeout",
        default=10.0,
        type=_positive_float,
        help="remote connection timeout in seconds (default: 10)",
    )
    parser.add_argument(
        "--buffer-size",
        default=DEFAULT_BUFFER_SIZE,
        type=_positive_int,
        help=f"copy buffer size in bytes (default: {DEFAULT_BUFFER_SIZE})",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    interface_index = None
    if args.interface != "system":
        try:
            interface_index = socket.if_nametoindex(args.interface)
        except OSError as exc:
            parser.error(f"network interface {args.interface!r} is unavailable: {exc}")

    config = RelayConfig(
        interface=args.interface,
        interface_index=interface_index,
        remote_host=args.remote_host,
        remote_port=args.remote_port,
        connect_timeout=args.connect_timeout,
        buffer_size=args.buffer_size,
        route_config=args.route_config,
    )

    try:
        with RelayServer((args.listen_host, args.listen_port), config) as server:
            listen_host, listen_port = server.server_address
            LOGGER.info(
                "listening on %s:%s; forwarding to %s:%s through %s (Ctrl-C to stop)",
                listen_host,
                listen_port,
                config.remote_host,
                config.remote_port,
                config.interface,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                LOGGER.info("interrupt received; stopping relay")
    except OSError as exc:
        LOGGER.error(
            "cannot listen on %s:%s: %s",
            args.listen_host,
            args.listen_port,
            exc,
        )
        return 1

    LOGGER.info("relay stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
