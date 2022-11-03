import json, trio, h11
from itertools import count
from wsgiref.handlers import format_date_time

from typing import List, Tuple, Dict, Set, Optional, Union, Any, cast, Type

H11Event = Type[h11._events._EventBundle]  # TODO: double-check this
H11Sentinel = Type[h11._util._SentinelBase] # TODO: double-check this

MAX_RECV = 2**16
TIMEOUT = 10

################################################################
# I/O adapter: h11 <-> trio
################################################################

class TrioHTTPConnection:
    """
    A wrapper around an h11.Connection, which hooks it up to a trio Stream.

    It:
      * reads incoming data into h11 events
      * sends any h11 events you give it
      * handles graceful shutdown
    """
    _next_id = count()

    def __init__(self, stream: trio.abc.HalfCloseableStream):
        self.stream = stream
        self.conn = h11.Connection(h11.SERVER)
        self.server_header = "whitelisting-proxy/1.0 ({h11.PRODUCT_ID})".encode()
        self._connection_id = next(TrioHTTPConnection._next_id)

    async def send(self, event: H11Event) -> None:
        if type(event) is h11.ConnectionClosed:
            assert self.conn.send(event) is None
            await self.shutdown_and_clean_up()
        else:
            data: Optional[bytes] = self.conn.send(event)
            assert data is not None
            await self.stream.send_all(data)

    async def _read_from_peer(self) -> None:
        """
        Reads some data from internal stream into the internal h11.Connection.
        """
        if self.conn.they_are_waiting_for_100_continue:
            self.info("Sending 100 Continue")
            go_ahead = h11.InformationalResponse(
                status_code=100, headers=self.basic_headers()
            )
            await self.send(go_ahead)
        try:
            data = await self.stream.receive_some(MAX_RECV)
        except ConnectionError:
            # They've stopped listening. Not much we can do about it here.
            data = b""
        self.conn.receive_data(data)

    async def next_event(self) -> Union[H11Event, H11Sentinel]:
        # Only two sentinels may be returned: h11.NEED_DATA and h11.PAUSED
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                await self._read_from_peer()
                continue
            return event

    async def shutdown_and_clean_up(self) -> None:
        try:
            await self.stream.send_eof()
        except trio.BrokenResourceError:
            # They're already gone, nothing to do
            return

        # Wait and read for a bit to give them a chance to see that we closed
        # things, but eventually give up and just close the socket.
        # XX FIXME: possibly we should set SO_LINGER to 0 here, so
        # that in the case where the client has ignored our shutdown and
        # declined to initiate the close themselves, we do a violent shutdown
        # (RST) and avoid the TIME_WAIT?
        # it looks like nginx never does this for keepalive timeouts, and only
        # does it for regular timeouts (slow clients I guess?) if explicitly
        # enabled ("Default: reset_timedout_connection off")
        with trio.move_on_after(TIMEOUT):
            try:
                while True:
                    # Attempt to read until EOF
                    got = await self.stream.receive_some(MAX_RECV)
                    if not got:
                        break
            except trio.BrokenResourceError:
                pass
            finally:
                await self.stream.aclose()

    def basic_headers(self) -> List[Tuple[bytes, bytes]]:  # h11._headers.Headers
        # HTTP requires these headers in all responses (client would do
        # something different here)
        return [
            (b"Date", format_date_time(None).encode("ascii")),
            (b"Server", self.server_header),
        ]

    def info(self, msg: str) -> None:
        print(f"{self._connection_id}: {msg}")

################################################################

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
                await send_error(w, 400, "")  # TODO: how to describe this, exactly?
                return

            if e.method != b"CONNECT":
                await send_error(w, 405, f"Method {e.method!r} is not allowed")
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
            await send_error(w, 400, "Malformed domain")
        elif number_of_colons == 1:
            host, port_bytes = target_host.split(b":")
            try:
                port = int(port_bytes)
                assert port in range(65536)
            except (ValueError, AssertionError):
                await send_error(w, 400, f"Invalid port number: {port_bytes!r}")
        else:
            host, port = target_host, 80

        if not is_whitelisted(host, port):
            await send_error(w, 403, "Host/port combination not allowed")

        w.info("Target host is allowed:" + str((host, port)))
        w.info(f'Making TCP connection to {host.decode("ascii")}:{port}')  # TODO: handle edge cases in decoding / input charset?

        with trio.fail_after(CONNECTION_TIMEOUT):
            try:
                target_stream = await trio.open_tcp_stream(host, port)
            except OSError as e:
                await send_error(w, 502, "TCP connection to server failed")
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
                await send_error(w, e.error_status_hint, str(e))
            elif type(e) is trio.TooSlowError:
                await send_error(w, 408, "Client is too slow, terminating connection")
            else:
                await send_error(w, 500, str(e))
        except Exception as e:
            import traceback
            w.info("Error while responding with 500 Internal Server Error: " + "\n".join(traceback.format_tb(e.__traceback__)))
            w.shutdown_and_clean_up()


async def send_error(w: TrioHTTPConnection, status_code: int, msg: str) -> None:
    """
    Send a JSON error message if possible.
    Otherwise, shut down the connection.
    """

    if w.conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
        # Cannot send an error; we can only terminate the connection.
        await w.shutdown_and_clean_up()
        return

    body = json.dumps({"error": msg}).encode("utf-8")

    headers = w.basic_headers() + [
        (b"Content-Length", b"%d" % len(body)),
        (b"Content-Type", b"application/json"),
    ]

    await w.send(h11.Response(status_code=status_code, headers=headers))
    await w.send(h11.Data(data=body))
    await w.send(h11.EndOfMessage())


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
