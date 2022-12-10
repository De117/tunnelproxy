import trio, random, threading, socket
import trio.testing, pytest
from datetime import timedelta
from functools import partial
from hypothesis import given, settings, HealthCheck, assume
from hypothesis.strategies import data, integers, binary, floats, lists, builds, sets, tuples

from typing import Set, Tuple, List

from whitelisting_proxy.proxy import splice


@given(integers(1, 100), data())
async def test_splice(num_iterations: int, data):
    client, near = trio.testing.memory_stream_pair()
    far, server = trio.testing.memory_stream_pair()
    
    async def spliceit(near, far):
        await splice(near, far)

    async def testit(a, b):
        for i in range(num_iterations):
            a, b = random.sample((a, b), k=2)  # global random, deterministic by hypothesis

            to_send = data.draw(binary(min_size=1))  # we must send something or receive_some will block
            await a.send_all(to_send)
            received = await b.receive_some(len(to_send))

            assert to_send == received

        a, b = random.sample((a, b), k=2)
        await a.aclose()  # kill one end, the rest should take care of itself


    async with trio.open_nursery() as nursery:
        nursery.start_soon(spliceit, near, far)
        nursery.start_soon(testit, client, server)


from whitelisting_proxy.proxy import WhitelistingProxy, run_synchronously_cancellable_proxy

# Thread scheduling varies, so we cannot reliably (nor quickly) test
# if SynchronousWhitelistingProxy cancellation works, i.e. that:
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

    p = WhitelistingProxy(whitelist=(),)
    stop = threading.Event()

    proxy_cancelled = False

    async def runner() -> None:
        nonlocal proxy_cancelled
        await run_synchronously_cancellable_proxy(p, host, port, stop, stop_check_interval)
        proxy_cancelled = True

    async def killer() -> None:
        await trio.to_thread.run_sync(stop.set)  # from another thread, as in SynchronousWhitelistingProxy
        assert stop.is_set(), "This should always be the case, as it's a threading.Event"
        await trio.sleep(1.001 * stop_check_interval)
        assert proxy_cancelled, "After `stop_check_interval`, the proxy should have been cancelled"

    async with trio.open_nursery() as nursery:
        nursery.start_soon(runner)
        nursery.start_soon(killer)

from whitelisting_proxy.proxy import Domain, Port, handle

# TESTS TO DO:
# ============
# Connect to a whitelisted hostname:
#   you should have access (200 OK)

# Connect to a non-whitelisted hostname:
#   you should get back a 403 error

# Connect to a whitelisted, but non-existent upstream:
#   you should get back a 502 error (if upstream is down)
#   you should get back a 504 error (if upstream is up but times out)

# Anything else should fail with a 4xx error
#   client timeouts: 408 (Too Slow)
#   bad method: 405 (Not Allowed)
#   malformed request is 400 (Bad Request)


################################################################
#            Generating valid domains and ports
################################################################
def new_label(length: int) -> str:
    """
    Return a "label" element according to RFC 1035, of specified length (>0).
    """
    if length <= 0:
        raise ValueError("There are no valid zero- or negative-length labels")
    letter = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    letter_digit = letter + "0123456789"
    letter_digit_hyphen = letter_digit + "-"

    if length == 1:
        label = random.choice(letter)
    else:
        label = (random.choice(letter)
            + "".join(random.choice(letter_digit_hyphen) for _ in range(length - 2))
            + random.choice(letter_digit)
        )
    return label

def new_domain(chunk_lengths: List[int]) -> Domain:
    """
    Return a valid domain according to RFC 1035.

    `chunk_lengths` must be a non-empty list of positive integers.
    """
    if not chunk_lengths or any(l <= 0 for l in chunk_lengths):
        raise ValueError()

    return Domain(".".join(new_label(l) for l in chunk_lengths))

def domains():
    return builds(new_domain, lists(integers(1, 10), min_size=1, max_size=10))

def ports(start: int = 1024, end: int = 65535):
    return builds(Port, integers(start, end))

@given(domains())
def test_domains(d: Domain) -> None:
    pass # We're testing example generation here.

@given(ports())
def test_ports(d: Port) -> None:
    pass # We're testing example generation here.

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


@given(domains=sets(domains(), min_size=1))
@settings(deadline=timedelta(seconds=1000), suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_handle(domains: Set[Domain], autojump_clock) -> None:

    # TODO: instead of using bytes directly, use an h11 client?
    async def connect_OK(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes) -> None:
        """Connect, assert it's OK, quit."""
        async with stream:
            await stream.send_all(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: whatever\r\n\r\n".encode())
            resp = await stream.receive_some(10000)
            assert resp.startswith(expected)

    async def connect_slowly(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes) -> None:
        async with stream:
            await trio.sleep(10)
            try:
                # This will blow up if the server already closed the connection.
                await stream.send_all(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: whatever\r\n\r\n".encode())
            except trio.BrokenResourceError:
                pass
            resp = await stream.receive_some(10000)
            assert resp.startswith(expected)

    async def connect_with_PUT(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes) -> None:
        async with stream:
            await stream.send_all(f"PUT {host}:{port} HTTP/1.1\r\nHost: whatever\r\n\r\n".encode())
            resp = await stream.receive_some(10000)
            assert resp.startswith(expected)

    async def connect_with_bytes(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes, to_send: bytes) -> None:
        async with stream:
            await stream.send_all(to_send)
            resp = await stream.receive_some(10000)
            assert resp.startswith(expected)

    async def accept_and_close_connection(s: trio.socket.SocketType) -> None:
        await s.accept()
        s.close()

    # We open a socket at an OS-chosen port, and override domain resolution.
    original_resolver = trio.socket.set_custom_hostname_resolver(ResolveAllToLocalhost())
    try:
        # Connect to a whitelisted hostname:
        #   you should have access (200 OK)
        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))
            sock.listen()  # type: ignore
            # (As of trio-typing 0.7.0, the type for listen() is wrong.)

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            whitelist = {(d, port) for d in domains}
            is_whitelisted = lambda host, port: (host, port) in whitelist

            # whitelisted hostname
            expected = b"HTTP/1.1 200 Connection established\r\n"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_OK, client_stream, host, port, expected)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)
                nursery.start_soon(accept_and_close_connection, sock)


        # Connect to a non-whitelisted hostname:
        #   you should get back a 403 error
        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))
            sock.listen()  # type: ignore
            # (As of trio-typing 0.7.0, the type for listen() is wrong.)

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            whitelist = {(d, port) for d in domains}
            is_whitelisted = lambda host, port: (host, port) in whitelist

            # non-whitelisted hostname
            expected = b"HTTP/1.1 403"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_OK, client_stream, "non-whitelisted.domain", port, expected)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)


        # Connect to a whitelisted, but non-existent upstream:
        #   you should get back a 502 error (if upstream is down)
        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))  # bound, but not listening

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            whitelist = {(d, port) for d in domains}
            is_whitelisted = lambda host, port: (host, port) in whitelist

            # whitelisted hostname
            expected = b"HTTP/1.1 502"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_OK, client_stream, host, port, expected)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)


        # Connect to a whitelisted, but non-existent upstream:
        #   you should get back a 504 error (if upstream is up but times out)
        #
        # XXX: we cannot use raw IP sockets as a normal user. But at least on
        #      UNIX-like systems, we can ensure a slow TCP handshake.

        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))
            sock.listen(0)  # type: ignore
            # (As of trio-typing 0.7.0, the type for listen() is wrong.)

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            # Setting the backlog to 0 does not actually set the number of
            # pending TCP connections to 0, but it comes close.
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

            # whitelisted hostname
            expected = b"HTTP/1.1 504"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_OK, client_stream, host, port, expected)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)


        # Anything else should fail with a 4xx error
        #   client timeouts: 408 (Too Slow)
        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))
            sock.listen()  # type: ignore
            # (As of trio-typing 0.7.0, the type for listen() is wrong.)

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            whitelist = {(d, port) for d in domains}
            is_whitelisted = lambda host, port: (host, port) in whitelist

            # whitelisted hostname
            expected = b"HTTP/1.1 408"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_slowly, client_stream, host, port, expected)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)


        #   bad method: 405 (Not Allowed)
        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))
            sock.listen()  # type: ignore
            # (As of trio-typing 0.7.0, the type for listen() is wrong.)

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            whitelist = {(d, port) for d in domains}
            is_whitelisted = lambda host, port: (host, port) in whitelist

            # whitelisted hostname
            expected = b"HTTP/1.1 405"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_with_PUT, client_stream, host, port, expected)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)

        #   malformed request is 400 (Bad Request)
        random_length = random.randint(0, 100)
        random_bytes = bytes(random.getrandbits(8) for _ in range(random_length))
        random_bytes += b"\r\n\r\n"  # we must terminate the line, or the server will time out

        with trio.socket.socket() as sock:
            await sock.bind(("localhost", 0))
            sock.listen()  # type: ignore
            # (As of trio-typing 0.7.0, the type for listen() is wrong.)

            _, port = sock.getsockname()
            host: Domain = random.choice(list(domains))

            whitelist = {(d, port) for d in domains}
            is_whitelisted = lambda host, port: (host, port) in whitelist

            expected = b"HTTP/1.1 400"
            client_stream, proxy_stream = trio.testing.memory_stream_pair()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(connect_with_bytes, client_stream, host, port, expected, random_bytes)
                nursery.start_soon(handle, proxy_stream, is_whitelisted)
    finally:
        trio.socket.set_custom_hostname_resolver(original_resolver)
