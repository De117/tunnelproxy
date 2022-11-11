import trio, random, threading
import trio.testing
from datetime import timedelta
from hypothesis import given, settings, HealthCheck
from hypothesis.strategies import data, integers, binary, floats

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
