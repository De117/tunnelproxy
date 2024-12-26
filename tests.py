import trio, random, threading, socket
import trio.testing, pytest
from datetime import timedelta
from functools import partial, wraps
from hypothesis import given, settings, HealthCheck, assume
from hypothesis.strategies import data, integers, binary, floats, lists, builds, sets, tuples, sampled_from, randoms

from typing import Set, Tuple, List, Callable

from tunnelproxy._proxy import splice


@given(integers(1, 100), data(), randoms())
async def test_splice(num_iterations: int, data, rand: random.Random):
    client, near = trio.testing.memory_stream_pair()
    far, server = trio.testing.memory_stream_pair()
    
    async def spliceit(near, far):
        await splice(near, far)

    async def testit(a, b):
        for i in range(num_iterations):
            a, b = rand.sample((a, b), k=2)

            to_send = data.draw(binary(min_size=1))  # we must send something or receive_some will block
            await a.send_all(to_send)
            received = await b.receive_some(len(to_send))

            assert to_send == received

        a, b = rand.sample((a, b), k=2)
        await a.aclose()  # kill one end, the rest should take care of itself


    async with trio.open_nursery() as nursery:
        nursery.start_soon(spliceit, near, far)
        nursery.start_soon(testit, client, server)


from tunnelproxy._proxy import TunnelProxy, run_synchronously_cancellable_proxy

# Thread scheduling varies, so we cannot reliably (nor quickly) test
# if SynchronousTunnelProxy cancellation works, i.e. that:
#
#     1. the proxy _will_ be cancelled
#     2. it will happen in no more than `stop_check_interval` seconds
#
# However, by putting the cancellation logic in a separate function,
# we can get most of the way there.

@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=timedelta(seconds=1))
@given(stop_check_interval=floats(0.001, 10.000))
async def test_cancellation_seen_promptly(stop_check_interval: float, autojump_clock):

    host = "localhost"
    port = 12349  # hopefully available

    p = TunnelProxy(allowed_hosts=(),)
    stop = threading.Event()

    proxy_cancelled = False

    async def runner() -> None:
        nonlocal proxy_cancelled
        await run_synchronously_cancellable_proxy(p, host, port, stop, stop_check_interval)
        proxy_cancelled = True

    async def killer() -> None:
        await trio.to_thread.run_sync(stop.set)  # from another thread, as in SynchronousTunnelProxy
        assert stop.is_set(), "This should always be the case, as it's a threading.Event"
        await trio.sleep(1.001 * stop_check_interval)
        assert proxy_cancelled, "After `stop_check_interval`, the proxy should have been cancelled"

    async with trio.open_nursery() as nursery:
        nursery.start_soon(runner)
        nursery.start_soon(killer)

from tunnelproxy._proxy import handle
from tunnelproxy._config import Domain, Port

# Tests for the main handler follow.
#
# Summary of tests for handle()
# =============================
# Connect to a whitelisted hostname:
#   you should have access (200 OK)
#
# Connect to a non-whitelisted hostname:
#   you should get back a 403 error
#
# Connect to a whitelisted, but non-existent upstream:
#   you should get back a 502 error (if upstream is down)
#   you should get back a 504 error (if upstream is up but times out)
#
# Anything else should fail with a 4xx error
#   client timeouts: 408 (Too Slow)
#   bad method: 405 (Not Allowed)
#   malformed request is 400 (Bad Request)
#
#
# But first, some helpers.


################################################################
#            Generating valid domains and ports
################################################################
def new_label(length: int, rand: random.Random) -> str:
    """
    Return a "label" element according to RFC 1035, of specified length (>0).
    """
    if length <= 0:
        raise ValueError("There are no valid zero- or negative-length labels")
    letter = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    letter_digit = letter + "0123456789"
    letter_digit_hyphen = letter_digit + "-"

    if length == 1:
        label = rand.choice(letter)
    else:
        label = (rand.choice(letter)
            + "".join(rand.choice(letter_digit_hyphen) for _ in range(length - 2))
            + rand.choice(letter_digit)
        )
    return label

def new_domain(chunk_lengths: List[int], rand: random.Random) -> Domain:
    """
    Return a valid domain according to RFC 1035.

    `chunk_lengths` must be a non-empty list of positive integers.
    """
    if not chunk_lengths or any(l <= 0 for l in chunk_lengths):
        raise ValueError()

    return Domain(".".join(new_label(l, rand) for l in chunk_lengths))

def domains():
    return builds(new_domain, lists(integers(1, 10), min_size=1, max_size=10), randoms())

def ports(start: int = 1024, end: int = 65535):
    return builds(Port, integers(start, end))

@given(domains())
def test_domains(d: Domain) -> None:
    pass # We're testing example generation here.

@given(ports())
def test_ports(d: Port) -> None:
    pass # We're testing example generation here.

################################################################
#                     Fake DNS resolution
################################################################

class ResolveAllToLocalhost(trio.abc.HostnameResolver):
    """A fake resolver, which resolves all hostnames to 127.0.0.1."""

    @staticmethod
    async def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # Synchronous, but should always return promptly.
        return socket.getaddrinfo("localhost", port, family, type, proto, flags)

    @staticmethod
    async def getnameinfo(self, sockaddr, flags):
        return await trio.socket.getnameinfo(sockaddr, flags)


def resolve_all_to_localhost(f: Callable) -> Callable:
    """Decorates an async function to run with ResolveAllToLocalhost() as the resolver."""
    @wraps(f)  # preserves function signature
    async def wrapper(*args, **kwargs):
        original_resolver = trio.socket.set_custom_hostname_resolver(ResolveAllToLocalhost())
        try:
            return (await f(*args, **kwargs))
        finally:
            trio.socket.set_custom_hostname_resolver(original_resolver)
    return wrapper

################################################################
#                  "HTTP client/server" functions
################################################################

async def connect(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes, method: str = "CONNECT") -> None:
    """Connect, assert it's OK, quit."""
    hostname = f"{host}:{port}"
    async with stream:
        await stream.send_all(f"{method} {hostname} HTTP/1.1\r\nHost: {hostname}\r\n\r\n".encode())
        resp = await stream.receive_some(10000)
        assert resp.startswith(expected)

async def connect_slowly(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes) -> None:
    """Like `connect`, does it very slowly."""
    async with stream:
        await trio.sleep(10)
        try:
            # This will blow up if the server already closed the connection.
            await stream.send_all(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: whatever\r\n\r\n".encode())
        except trio.BrokenResourceError:
            pass
        resp = await stream.receive_some(10000)
        assert resp.startswith(expected)

async def connect_with_bytes(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes, to_send: bytes) -> None:
    """Like `connect`, but sends the given bytes instead of an actual HTTP request."""
    async with stream:
        await stream.send_all(to_send)
        resp = await stream.receive_some(10000)
        assert resp.startswith(expected)

async def accept_and_close_connection(s: trio.socket.SocketType) -> None:
    await s.accept()
    s.close()

################################################################
#               Actual tests for handle()
################################################################

@given(domains=sets(domains(), min_size=1), rand=randoms())
@resolve_all_to_localhost
async def test_connect_to_whitelisted_host(domains: Set[Domain], rand: random.Random) -> None:

    expected = b"HTTP/1.1 200"

    # Connect to a whitelisted hostname:
    #   you should have access (200 OK)
    with trio.socket.socket() as sock:
        await sock.bind(("localhost", 0))
        sock.listen()

        _, port = sock.getsockname()
        host: Domain = rand.choice(list(domains))

        whitelist = {(d, port) for d in domains}
        is_whitelisted = lambda host, port: (host, port) in whitelist

        client_stream, proxy_stream = trio.testing.memory_stream_pair()
        async with trio.open_nursery() as nursery:
            nursery.start_soon(connect, client_stream, host, port, expected)
            nursery.start_soon(handle, proxy_stream, is_whitelisted)
            nursery.start_soon(accept_and_close_connection, sock)


@given(domains=sets(domains(), min_size=1), port=ports())
@resolve_all_to_localhost
async def test_connect_to_non_whitelisted_host(domains: Set[Domain], port: Port) -> None:

    expected = b"HTTP/1.1 403"

    # Connect to a non-whitelisted hostname:
    #   you should get back a 403 error
    whitelist = {(d, port) for d in domains}
    is_whitelisted = lambda host, port: (host, port) in whitelist

    client_stream, proxy_stream = trio.testing.memory_stream_pair()
    async with trio.open_nursery() as nursery:
        nursery.start_soon(connect, client_stream, "non-whitelisted.domain", port, expected)
        nursery.start_soon(handle, proxy_stream, is_whitelisted)


@given(domains=sets(domains(), min_size=1), port=ports(), rand=randoms())
@resolve_all_to_localhost
async def test_connect_to_whitelisted_nonexistent_upstream_host(domains: Set[Domain], port: Port, rand: random.Random) -> None:

    expected = b"HTTP/1.1 502"

    # Connect to a whitelisted, but non-existent upstream:
    #   you should get back a 502 error (if upstream is down)
    host: Domain = rand.choice(list(domains))
    whitelist = {(d, port) for d in domains}
    is_whitelisted = lambda host, port: (host, port) in whitelist

    client_stream, proxy_stream = trio.testing.memory_stream_pair()
    async with trio.open_nursery() as nursery:
        nursery.start_soon(connect, client_stream, host, port, expected)
        nursery.start_soon(handle, proxy_stream, is_whitelisted)


@given(domains=sets(domains(), min_size=1), rand=randoms())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@resolve_all_to_localhost
async def test_connect_to_whitelisted_slow_upstream_host(domains: Set[Domain], rand: random.Random, autojump_clock) -> None:

    expected = b"HTTP/1.1 504"

    # Connect to a whitelisted, but non-existent upstream:
    #   you should get back a 504 error (if upstream is up but times out)
    #
    # XXX: we cannot use raw IP sockets as a normal user. But at least on
    #      UNIX-like systems, we can ensure a slow TCP handshake.
    with trio.socket.socket() as sock:
        await sock.bind(("localhost", 0))
        sock.listen(0)

        _, port = sock.getsockname()
        host: Domain = rand.choice(list(domains))

        # Setting the listen backlog to 0 does not actually set the
        # number of pending TCP connections to 0, but it comes close.
        #
        # So if other TCP connections come first, our handshake will be
        # merely delayed, and will not get a RST in return, as it would
        # if there was no process listening.
        stuffing_sockets = []
        while True:
            s = trio.socket.socket()
            with trio.move_on_after(0.010) as cancel_scope:
                await s.connect(("localhost", port))
            if cancel_scope.cancelled_caught:
                break  # that's enough, it can't fit any more
            stuffing_sockets.append(s)

        whitelist = {(d, port) for d in domains}
        is_whitelisted = lambda host, port: (host, port) in whitelist

        client_stream, proxy_stream = trio.testing.memory_stream_pair()
        async with trio.open_nursery() as nursery:
            nursery.start_soon(connect, client_stream, host, port, expected)
            nursery.start_soon(handle, proxy_stream, is_whitelisted)


@given(domains=sets(domains(), min_size=1), port=ports(), rand=randoms())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@resolve_all_to_localhost
async def test_connect_where_client_times_out(domains: Set[Domain], port: Port, rand: random.Random, autojump_clock) -> None:

    expected = b"HTTP/1.1 408"

    # Anything else should fail with a 4xx error
    #   client timeouts: 408 (Too Slow)
    host: Domain = rand.choice(list(domains))
    whitelist = {(d, port) for d in domains}
    is_whitelisted = lambda host, port: (host, port) in whitelist

    client_stream, proxy_stream = trio.testing.memory_stream_pair()
    async with trio.open_nursery() as nursery:
        nursery.start_soon(connect_slowly, client_stream, host, port, expected)
        nursery.start_soon(handle, proxy_stream, is_whitelisted)


HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"]

@given(domains=sets(domains(), min_size=1), port=ports(), method=sampled_from(HTTP_METHODS + ["DJWAODUIJAW"]), rand=randoms())
@resolve_all_to_localhost
async def test_connect_with_bad_method(domains: Set[Domain], port: Port, method: str, rand: random.Random) -> None:

    assume(method != "CONNECT")
    expected = b"HTTP/1.1 405"

    #   bad method: 405 (Not Allowed)
    host: Domain = rand.choice(list(domains))
    whitelist = {(d, port) for d in domains}
    is_whitelisted = lambda host, port: (host, port) in whitelist

    client_stream, proxy_stream = trio.testing.memory_stream_pair()
    async with trio.open_nursery() as nursery:
        nursery.start_soon(connect, client_stream, host, port, expected, method)
        nursery.start_soon(handle, proxy_stream, is_whitelisted)


@given(domains=sets(domains(), min_size=1), port=ports(), rand=randoms())
@resolve_all_to_localhost
async def test_connect_with_random_input(domains: Set[Domain], port: Port, rand: random.Random) -> None:

    expected = b"HTTP/1.1 400"

    #   malformed request: 400 (Bad Request)
    random_length = rand.randint(0, 100)
    random_bytes = bytes(rand.getrandbits(8) for _ in range(random_length))
    random_bytes += b"\r\n\r\n"  # we must terminate the line, or the server will time out

    host: Domain = rand.choice(list(domains))
    whitelist = {(d, port) for d in domains}
    is_whitelisted = lambda host, port: (host, port) in whitelist

    client_stream, proxy_stream = trio.testing.memory_stream_pair()
    async with trio.open_nursery() as nursery:
        # As of trio-typing 0.10.0, start_soon's type signature only supports up to 5 arguments.
        nursery.start_soon(connect_with_bytes, client_stream, host, port, expected, random_bytes)  # type: ignore
        nursery.start_soon(handle, proxy_stream, is_whitelisted)
