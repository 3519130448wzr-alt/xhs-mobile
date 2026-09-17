"""Local socketpair/loopback tests; never contact or restart the device relay."""

import importlib.util
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, call

import pytest

RELAY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "adb_interface_relay.py"
RELAY_SPEC = importlib.util.spec_from_file_location("_test_adb_interface_relay", RELAY_PATH)
assert RELAY_SPEC is not None and RELAY_SPEC.loader is not None
RELAY_MODULE = importlib.util.module_from_spec(RELAY_SPEC)
# dataclasses resolves the module by name while the standalone script loads.
sys.modules[RELAY_SPEC.name] = RELAY_MODULE
RELAY_SPEC.loader.exec_module(RELAY_MODULE)
_relay_bidirectionally = RELAY_MODULE._relay_bidirectionally


class ObservedSocket:
    def __init__(self, raw, *, short_writes=None):
        self.raw = raw
        self.short_writes = short_writes
        self.write_started = threading.Event()
        self.send_sizes = []
        self.recv_sizes = []
        self.shutdowns = []

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def recv(self, size):
        self.recv_sizes.append(size)
        return self.raw.recv(size)

    def send(self, data):
        self.write_started.set()
        self.send_sizes.append(len(data))
        return self.raw.send(data[:self.short_writes] if self.short_writes else data)

    def sendall(self, data):
        # Supports exercising the previous blocking implementation as well.
        self.write_started.set()
        return self.raw.sendall(data)

    def shutdown(self, how):
        self.shutdowns.append(how)
        return self.raw.shutdown(how)


class LocalRelay:
    def __init__(self, buffer_size=65536, short_writes=None):
        self.client, self.client_side = socket.socketpair()
        self.server_side, self.server = socket.socketpair()
        self.server_side.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.client_proxy = ObservedSocket(self.client_side, short_writes=short_writes)
        self.server_proxy = ObservedSocket(self.server_side, short_writes=short_writes)
        self.errors = []
        self.buffer_size = buffer_size
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            _relay_bidirectionally(
                self.client_proxy, self.server_proxy, buffer_size=self.buffer_size
            )
        except OSError as exc:
            self.errors.append(exc)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        for endpoint in (self.client, self.server):
            try:
                endpoint.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(timeout=2)
        for endpoint in (self.client, self.client_side, self.server_side, self.server):
            endpoint.close()
        assert not self.thread.is_alive(), "Local relay failed to terminate"


def receive_exact(endpoint, size):
    received = bytearray()
    endpoint.settimeout(3)
    while len(received) < size:
        chunk = endpoint.recv(size - len(received))
        assert chunk, "Premature EOF while draining synthetic payload"
        received.extend(chunk)
    return bytes(received)


def test_upload_backpressure_does_not_block_reverse_control_reply():
    with LocalRelay() as relay:
        payload = b"SYNTHETIC-APK-BYTES" * (128 * 1024)
        writer_errors = []

        def upload():
            try:
                relay.client.sendall(payload)
                relay.client.shutdown(socket.SHUT_WR)
            except OSError as exc:
                writer_errors.append(exc)

        writer = threading.Thread(target=upload, daemon=True)
        writer.start()
        try:
            assert relay.server_proxy.write_started.wait(2)
            # The receiver deliberately does not drain the upload yet. Its own
            # independent response must still reach the uploading peer.
            marker = b"SYNTHETIC-REVERSE-CONTROL-REPLY"
            relay.server.sendall(marker)
            assert receive_exact(relay.client, len(marker)) == marker
            assert writer.is_alive(), "Test did not establish upload backpressure"
            assert receive_exact(relay.server, len(payload)) == payload
            assert relay.server.recv(1) == b""
            relay.server.shutdown(socket.SHUT_WR)
            assert relay.client.recv(1) == b""
            writer.join(timeout=2)
            assert not writer.is_alive()
            assert not writer_errors and not relay.errors
        finally:
            if writer.is_alive():
                relay.client.shutdown(socket.SHUT_RDWR)
                writer.join(timeout=2)


@pytest.mark.parametrize("buffer_size,short_writes", [(1, 1), (31, 3), (4096, 17)])
def test_partial_writes_preserve_data_and_flush_before_half_close(buffer_size, short_writes):
    with LocalRelay(buffer_size, short_writes=short_writes) as relay:
        request = bytes(range(251)) * 3
        response = bytes(reversed(range(251))) * 2
        relay.client.sendall(request)
        relay.client.shutdown(socket.SHUT_WR)
        assert receive_exact(relay.server, len(request)) == request
        assert relay.server.recv(1) == b""
        # A request EOF must not close the independent response direction.
        relay.server.sendall(response)
        relay.server.shutdown(socket.SHUT_WR)
        assert receive_exact(relay.client, len(response)) == response
        assert relay.client.recv(1) == b""
        relay.thread.join(timeout=2)
        assert not relay.thread.is_alive()
        assert not relay.errors
        for endpoint in (relay.client_proxy, relay.server_proxy):
            assert max(endpoint.send_sizes) <= buffer_size
            assert max(endpoint.recv_sizes) <= buffer_size
            assert endpoint.shutdowns.count(socket.SHUT_WR) == 1


def test_empty_bidirectional_eof_exits_without_writes():
    with LocalRelay() as relay:
        relay.client.shutdown(socket.SHUT_WR)
        relay.server.shutdown(socket.SHUT_WR)
        relay.client.settimeout(2)
        relay.server.settimeout(2)
        assert relay.client.recv(1) == b""
        assert relay.server.recv(1) == b""
        relay.thread.join(timeout=2)
        assert not relay.thread.is_alive()
        assert not relay.client_proxy.send_sizes and not relay.server_proxy.send_sizes
        assert not relay.errors


def test_nonpositive_buffer_is_rejected_without_socket_operations():
    with pytest.raises(ValueError, match="positive"):
        _relay_bidirectionally(None, None, buffer_size=0)


def synthetic_config(interface="system", interface_index=None):
    return RELAY_MODULE.RelayConfig(
        interface=interface, interface_index=interface_index,
        remote_host="SYNTHETIC.invalid", remote_port=43210,
        connect_timeout=2.0, buffer_size=1024,
    )


def fake_addresses(monkeypatch, count=1):
    # TEST-NET addresses only; all network sockets below are test doubles.
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
         (f"192.0.2.{index + 10}", 43210))
        for index in range(count)
    ]
    lookup = Mock(return_value=addresses)
    monkeypatch.setattr(RELAY_MODULE.socket, "getaddrinfo", lookup)
    return addresses, lookup


@pytest.mark.parametrize("interface,index", [("system", None), ("SYNTHETIC-en0", 7)])
def test_explicit_route_parameter_and_interface_lookup(monkeypatch, interface, index):
    monkeypatch.setattr(sys, "argv", [
        "SYNTHETIC-relay", "--interface", interface,
        "--remote-host", "SYNTHETIC.invalid", "--remote-port", "43210",
    ])
    lookup = Mock(return_value=index)
    monkeypatch.setattr(RELAY_MODULE.socket, "if_nametoindex", lookup)
    instance = Mock(server_address=("127.0.0.1", 6100))
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=instance)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(RELAY_MODULE, "RelayServer", factory)
    assert RELAY_MODULE.main() == 0
    assert factory.call_args.args[0] == ("127.0.0.1", 6100)
    config = factory.call_args.args[1]
    assert config.interface == interface and config.interface_index == index
    if interface == "system":
        lookup.assert_not_called()
    else:
        lookup.assert_called_once_with(interface)
    instance.serve_forever.assert_called_once()


def test_system_routing_never_binds_the_outbound_socket(monkeypatch):
    addresses, lookup = fake_addresses(monkeypatch)
    outbound = Mock()
    factory = Mock(return_value=outbound)
    monkeypatch.setattr(RELAY_MODULE.socket, "socket", factory)
    binding = Mock(side_effect=AssertionError("SYNTHETIC system route must not bind"))
    monkeypatch.setattr(RELAY_MODULE, "_bind_to_interface", binding)
    assert RELAY_MODULE._connect_remote(synthetic_config()) is outbound
    lookup.assert_called_once_with(
        "SYNTHETIC.invalid", 43210, family=socket.AF_INET,
        type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
    )
    binding.assert_not_called()
    outbound.setsockopt.assert_called_once_with(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    outbound.connect.assert_called_once_with(addresses[0][4])
    assert outbound.settimeout.call_args_list == [call(2.0), call(None)]
    outbound.close.assert_not_called()


def test_named_darwin_interface_is_bound_before_connect(monkeypatch):
    addresses, _ = fake_addresses(monkeypatch)
    monkeypatch.setattr(RELAY_MODULE.sys, "platform", "darwin")
    outbound = Mock()
    monkeypatch.setattr(RELAY_MODULE.socket, "socket", Mock(return_value=outbound))
    assert RELAY_MODULE._connect_remote(synthetic_config("SYNTHETIC-en0", 7)) is outbound
    assert outbound.method_calls == [
        call.settimeout(2.0),
        call.setsockopt(socket.IPPROTO_IP, RELAY_MODULE.DARWIN_IP_BOUND_IF, 7),
        call.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        call.connect(addresses[0][4]), call.settimeout(None),
    ]


@pytest.mark.parametrize("failure", ["binding", "connect"])
def test_named_interface_failure_never_falls_back_to_an_unbound_socket(monkeypatch, failure):
    addresses, _ = fake_addresses(monkeypatch, count=2)
    monkeypatch.setattr(RELAY_MODULE.sys, "platform", "darwin")
    sockets = [Mock(), Mock()]
    for outbound in sockets:
        operation = outbound.setsockopt if failure == "binding" else outbound.connect
        operation.side_effect = OSError(49, "SYNTHETIC cannot assign requested address")
    factory = Mock(side_effect=sockets)
    monkeypatch.setattr(RELAY_MODULE.socket, "socket", factory)
    with pytest.raises(RELAY_MODULE.RelayError, match="SYNTHETIC-en0"):
        RELAY_MODULE._connect_remote(synthetic_config("SYNTHETIC-en0", 7))
    assert factory.call_count == len(addresses)
    for outbound in sockets:
        expected_options = [call(socket.IPPROTO_IP, RELAY_MODULE.DARWIN_IP_BOUND_IF, 7)]
        if failure == "connect":
            expected_options.append(call(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
        assert outbound.setsockopt.call_args_list == expected_options
        if failure == "binding":
            outbound.connect.assert_not_called()
        else:
            outbound.connect.assert_called_once()
        outbound.close.assert_called_once()


def test_tcp_relay_disables_nagle_on_both_sides_and_preserves_half_close(monkeypatch):
    """Exercise actual TCP options and framing, without timing-flaky speed claims."""
    observed_options = []
    relay_finished = threading.Event()

    def observe_tcp_options(client, remote, *, buffer_size):
        observed_options.extend(
            endpoint.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
            for endpoint in (client, remote)
        )
        try:
            _relay_bidirectionally(client, remote, buffer_size=buffer_size)
        finally:
            relay_finished.set()

    monkeypatch.setattr(RELAY_MODULE, "_relay_bidirectionally", observe_tcp_options)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        config = RELAY_MODULE.replace(
            synthetic_config(), remote_host="127.0.0.1", remote_port=listener.getsockname()[1],
        )
        with RELAY_MODULE.RelayServer(("127.0.0.1", 0), config) as server:
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
            )
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=3) as client:
                    remote, _ = listener.accept()
                    with remote:
                        # Split writes represent an interactive framed stream;
                        # coalescing is not needed to preserve these bytes.
                        client.sendall(b"SYNTHETIC-HEADER")
                        client.sendall(b"SYNTHETIC-BODY")
                        client.shutdown(socket.SHUT_WR)
                        assert receive_exact(remote, 30) == b"SYNTHETIC-HEADERSYNTHETIC-BODY"
                        assert remote.recv(1) == b""
                        remote.sendall(b"SYNTHETIC-REPLY")
                        remote.shutdown(socket.SHUT_WR)
                        assert receive_exact(client, 15) == b"SYNTHETIC-REPLY"
                        assert client.recv(1) == b""
                        assert relay_finished.wait(3)
                        # Darwin returns its enabled flag bit (4), while other
                        # kernels may return 1. Both must be nonzero.
                        assert len(observed_options) == 2 and all(observed_options)
            finally:
                server.shutdown()
                thread.join(timeout=3)
                assert not thread.is_alive()


def test_unavailable_named_interface_rejected_before_listening(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "SYNTHETIC-relay", "--interface", "SYNTHETIC-missing",
        "--remote-host", "SYNTHETIC.invalid", "--remote-port", "43210",
    ])
    lookup = Mock(side_effect=OSError("SYNTHETIC unavailable"))
    monkeypatch.setattr(RELAY_MODULE.socket, "if_nametoindex", lookup)
    server = Mock(side_effect=AssertionError("Invalid route must not start server"))
    monkeypatch.setattr(RELAY_MODULE, "RelayServer", server)
    with pytest.raises(SystemExit) as error:
        RELAY_MODULE.main()
    assert error.value.code == 2
    lookup.assert_called_once_with("SYNTHETIC-missing")
    server.assert_not_called()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::1", "localhost", "not-an-address"])
def test_non_loopback_listener_rejected_before_socket_creation(host, monkeypatch):
    arguments = ["--interface", "system", "--remote-host", "SYNTHETIC.invalid",
                 "--remote-port", "43210", "--listen-host", host]
    with pytest.raises(SystemExit) as error:
        RELAY_MODULE._build_parser().parse_args(arguments)
    assert error.value.code == 2
    factory = Mock(side_effect=AssertionError("Invalid listener must not create socket"))
    monkeypatch.setattr(RELAY_MODULE.socket, "socket", factory)
    with pytest.raises(RELAY_MODULE.argparse.ArgumentTypeError, match="loopback"):
        RELAY_MODULE.RelayServer((host, 6100), synthetic_config())
    factory.assert_not_called()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2"])
def test_loopback_listener_parameter_is_preserved(host):
    args = RELAY_MODULE._build_parser().parse_args([
        "--interface", "system", "--remote-host", "SYNTHETIC.invalid",
        "--remote-port", "43210", "--listen-host", host,
    ])
    assert args.listen_host == host and args.interface == "system"


def test_dynamic_route_is_loaded_per_connection_without_retargeting(tmp_path, monkeypatch):
    route = tmp_path / "SYNTHETIC-route.json"
    route.write_text('{"interface":"system"}')
    route.chmod(0o600)
    config = RELAY_MODULE.replace(synthetic_config(), route_config=str(route))
    lookup = Mock(return_value=99)
    monkeypatch.setattr(RELAY_MODULE.socket, "if_nametoindex", lookup)
    first = RELAY_MODULE._current_config(config)
    assert first.interface == "system" and first.interface_index is None
    lookup.assert_not_called()
    route.write_text('{"interface":"en9"}')
    second = RELAY_MODULE._current_config(config)
    assert second.interface == "en9" and second.interface_index == 99
    assert second.remote_host == config.remote_host and second.remote_port == config.remote_port
    assert config.interface == "system"  # Already open connections retain their immutable config.


@pytest.mark.parametrize("document", [
    '{"interface":"utun5"}', '{"interface":"en0;echo bad"}',
    '{"interface":"system","remote_port":5555}', '{"interface":null}', "not-json",
])
def test_dynamic_invalid_route_fails_closed(tmp_path, document):
    route = tmp_path / "SYNTHETIC-route.json"
    route.write_text(document)
    route.chmod(0o600)
    config = RELAY_MODULE.replace(synthetic_config(), route_config=str(route))
    with pytest.raises(RELAY_MODULE.RelayError):
        RELAY_MODULE._current_config(config)


def test_dynamic_missing_or_nonprivate_route_never_falls_back(tmp_path):
    route = tmp_path / "SYNTHETIC-route.json"
    config = RELAY_MODULE.replace(synthetic_config(), route_config=str(route))
    with pytest.raises(RELAY_MODULE.RelayError):
        RELAY_MODULE._current_config(config)
    route.write_text('{"interface":"system"}')
    route.chmod(0o644)
    with pytest.raises(RELAY_MODULE.RelayError):
        RELAY_MODULE._current_config(config)


def test_dynamic_named_interface_disappearing_does_not_use_system(tmp_path, monkeypatch):
    route = tmp_path / "SYNTHETIC-route.json"
    route.write_text('{"interface":"en99"}')
    route.chmod(0o600)
    config = RELAY_MODULE.replace(synthetic_config(), route_config=str(route))
    monkeypatch.setattr(RELAY_MODULE.socket, "if_nametoindex", Mock(side_effect=OSError()))
    with pytest.raises(RELAY_MODULE.RelayError):
        RELAY_MODULE._current_config(config)
