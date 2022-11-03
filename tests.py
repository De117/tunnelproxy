import trio, random
import trio.testing
from hypothesis import given, settings
from hypothesis.strategies import data, integers, binary

from proxy import splice


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
