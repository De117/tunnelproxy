import trio
import quart, quart_trio
from urllib.parse import urlparse

whitelist = ["www.example.com", "localhost"]

app = quart_trio.QuartTrio("domain-whitelisting-proxy")

@app.route("/proxy", methods=["POST"])
async def handle_request():

    parameters = await quart.request.form
    if not "url" in parameters:
        return {"error": f"the <url> parameter must be present"}, 400
    url = parameters["url"]

    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        return {"error": f"scheme {scheme} is not http or https"}, 400

    domain = urlparse(url).netloc
    if domain.count(":") > 1:
        return {"error": f"domain {domain} is malformed"}, 400
    elif ":" in domain:
        domain, portstring = domain.split(":")
        port = int(portstring)
    else:
        port = 80 if scheme == "http" else 443

    if domain not in whitelist:
        return {"error": f"domain {domain} is not whitelisted"}, 403

    try:
        s = trio.socket.socket()
        await s.connect((domain, port))
    except OSError as e:
        return {"error": f"Connection to upstream server failed: {e}"}, 502

    import ipdb; ipdb.set_trace()
    client_request_socket = ...  # FIXME: this cannot be fixed.
                                 # ASGI offers no way to get the client socket,
                                 # and Hypercorn implements it in such a way that
                                 # we cannot even get it by digging through its internals.
    await splice(client_request_socket, s)


async def splice(
        s1: trio.socket.SocketType,
        s2: trio.socket.SocketType,
    ) -> None:
    """
    "Splices" two TCP sockets into a single stream.

    That is, it forwards everything from s1 to s2, and vice versa.

    When one part of the connection breaks or finishes, it cleans up
    the other one and returns.
    """
    async with trio.SocketStream(s1) as a:
        async with trio.SocketStream(s2) as b:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(forward, a, b)
                nursery.start_soon(forward, b, a)

async def forward(source: trio.SocketStream, sink: trio.SocketStream) -> None:
    while True:
        try:
            chunk = await source.receive_some(max_bytes=16384)
            await sink.send_all(chunk)
        except (trio.BrokenResourceError, trio.ClosedResourceError):
            return  # clean up
