import trio, random, threading
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

# TESTS:
# ======
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

@given(sets(tuples(domains(), ports()), min_size=1))
async def test_full_connect(whitelist: Set[Tuple[Domain, Port]]) -> None:

    host, port = next(iter(whitelist))
    is_whitelisted = lambda host, port: (host, port) in set(whitelist)

    # TODO: instead of using bytes directly, use an h11 client?
    async def connect_OK(stream: trio.abc.Stream, host: Domain, port: Port, expected: bytes) -> None:
        """Connect, assert it's OK, quit."""
        async with stream:
            await stream.send_all(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: whatever\r\n\r\n".encode())
            resp = await stream.receive_some(10000)
            assert resp.startswith(expected)

    expected = b"HTTP/1.1 200 Connection established\r\n"

    # Instead of providing an external server, we stub trio.open_tcp_stream.

    # TODO: do this better.
    # Currently it is too tied to the implementation of `handle`,
    # and is far from obviously correct.
    client_stream, near_proxy_stream = trio.testing.memory_stream_pair()
    far_proxy_stream, server_stream = trio.testing.memory_stream_pair()
    try:
        original_trio_open_tcp_stream = trio.open_tcp_stream

        async def fake_open_tcp_stream(*args, **kwargs):
            return far_proxy_stream
        trio.open_tcp_stream = fake_open_tcp_stream

        async with trio.open_nursery() as nursery:
            nursery.start_soon(connect_OK, client_stream, host, port, expected)
            nursery.start_soon(handle, near_proxy_stream, is_whitelisted)
    finally:
        trio.open_tcp_stream = original_trio_open_tcp_stream
