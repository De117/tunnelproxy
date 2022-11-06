import trio, h11
from adapter import TrioHTTPConnection

def is_whitelisted(host: bytes, port: int) -> bool:
    whitelist = [
        (b"example.com", 80),
        (b"example.com", 443),
        (b"www.example.com", 80),
        (b"www.example.com", 443),
        (b"localhost", 12345),
    ]
    return (host, port) in whitelist


async def handle(stream: trio.SocketStream) -> None:
    w = TrioHTTPConnection(stream)

    try:
        assert w.conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}

        REQUEST_TIMEOUT = 5
        CONNECTION_TIMEOUT = 5

        with trio.fail_after(REQUEST_TIMEOUT):
            # 1. Request (= start of request)
            # 2. Data* (optional)
            # 3. EndOfMessage (= end of request)
            # at any moment: ConnectionClosed or exception
            e = await w.next_event()
            w.info(f"Got event: {e}")

            if type(e) is not h11.Request:
                await w.send_error(400, "")  # TODO: how to describe this, exactly?
                return

            if e.method != b"CONNECT":
                await w.send_error(405, f"Method {e.method!r} is not allowed")
                return

            target_host = e.target  # parse later

            # Ignore any HTTP body (h11.Data entries)
            # and read until h11.EndOfMessage
            while type(e := await w.next_event()) is not h11.EndOfMessage:
                w.info(f"Got event: {e}")
                pass
            w.info(f"Got event: {e}")

        number_of_colons = target_host.count(b":")
        if number_of_colons > 1:
            await w.send_error(400, "Malformed domain")
        elif number_of_colons == 1:
            host, port_bytes = target_host.split(b":")
            try:
                port = int(port_bytes)
                assert port in range(65536)
            except (ValueError, AssertionError):
                await w.send_error(400, f"Invalid port number: {port_bytes!r}")
        else:
            host, port = target_host, 80

        if not is_whitelisted(host, port):
            await w.send_error(403, "Host/port combination not allowed")

        w.info("Target host is allowed:" + str((host, port)))
        w.info(f'Making TCP connection to {host.decode("ascii")}:{port}')  # TODO: handle edge cases in decoding / input charset?

        with trio.fail_after(CONNECTION_TIMEOUT):
            try:
                target_stream = await trio.open_tcp_stream(host, port)
            except OSError as e:
                await w.send_error(502, "TCP connection to server failed")
                return
            else:
                # All good!
                # Send a plain 200 OK, which will switch protocols.
                await w.send(h11.Response(status_code=200, reason="Connection established", headers=w.basic_headers()))
                assert w.conn.our_state == w.conn.their_state == h11.SWITCHED_PROTOCOL
        
        await splice(stream, target_stream)
        w.info("TCP connection ended")

    except Exception as e:
        w.info(f"Caught exception: {e}")
        try:
            if type(e) is h11.RemoteProtocolError:
                await w.send_error(e.error_status_hint, str(e))
            elif type(e) is trio.TooSlowError:
                await w.send_error(408, "Client is too slow, terminating connection")
            else:
                await w.send_error(500, str(e))
        except Exception as e:
            import traceback
            w.info("Error while responding with 500 Internal Server Error: " + "\n".join(traceback.format_tb(e.__traceback__)))
            w.shutdown_and_clean_up()


async def splice(a: trio.SocketStream, b: trio.SocketStream) -> None:
    """
    "Splices" two TCP streams into one.
    That is, it forwards everything from a to b, and vice versa.

    When one part of the connection breaks or finishes, it cleans up
    the other one and returns.
    """
    async with a:
        async with b:
            async with trio.open_nursery() as nursery:
                # From RFC 7231, §4.3.6:
                # ----------------------
                # A tunnel is closed when a tunnel intermediary detects that
                # either side has closed its connection: the intermediary MUST
                # attempt to send any outstanding data that came from the
                # closed side to the other side, close both connections,
                # and then discard any remaining data left undelivered.

                # This holds, because the coroutines below run until one tries
                # to read from a closed socket, at which point both are cancelled.
                nursery.start_soon(forward, a, b, nursery.cancel_scope)
                nursery.start_soon(forward, b, a, nursery.cancel_scope)


async def forward(source: trio.SocketStream, sink: trio.SocketStream, cancel_scope: trio.CancelScope) -> None:
    while True:
        try:
            chunk = await source.receive_some(max_bytes=16384)
            if chunk:
                await sink.send_all(chunk)
            else:
                break  # nothing more to read
        except (trio.BrokenResourceError, trio.ClosedResourceError):
            break

    cancel_scope.cancel()
    return


async def serve(port):
    print("listening on http://localhost:{}".format(port))
    try:
        await trio.serve_tcp(handle, port)
    except KeyboardInterrupt:
        print("KeyboardInterrupt - shutting down")


if __name__ == "__main__":
    trio.run(serve, 8080)
