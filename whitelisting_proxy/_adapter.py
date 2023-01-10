import trio, h11, json, random
from wsgiref.handlers import format_date_time
from typing import List, Tuple, Optional, Union, Type

################################################################
# I/O adapter: h11 <-> trio
################################################################

MAX_RECV = 2**16

class TrioHTTPConnection:
    """
    A wrapper around a server h11.Connection, which hooks it up to a trio Stream.

    It:
      * reads incoming data into h11 events
      * sends any h11 events you give it
      * handles graceful shutdown
    """

    def __init__(self, stream: trio.abc.HalfCloseableStream, shutdown_timeout: float = 10):
        """
        shutdown_timeout: seconds to wait before closing the TCP connection, after sending EOF to the client
        """
        self.stream = stream
        self.conn = h11.Connection(h11.SERVER)
        self.server_header = f"whitelisting-proxy/1.0 ({h11.PRODUCT_ID})".encode()
        self._connection_id = hex(random.getrandbits(64))[2:].zfill(16)
        self.shutdown_timeout = shutdown_timeout

    async def send(self, event: h11.Event) -> None:
        if type(event) is h11.ConnectionClosed:
            assert self.conn.send(event) is None
            await self.ensure_shutdown()
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

    async def next_event(self) -> Union[h11.Event, Type[h11.NEED_DATA], Type[h11.PAUSED]]:
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                await self._read_from_peer()
                continue
            return event

    async def ensure_shutdown(self) -> None:
        """
        Terminates the connection. Idempotent.
        """
        # On the happy path, the client sends data only when he's supposed to,
        # and the server has already done any closure on the application layer.
        # So, we can close the TCP connection, and the client (seeing the FIN
        # and the application-layer close) shall close his side, and all is OK.

        # Unhappy paths:
        #
        #   1. Client keeps sending data, and ignores (or does not notice) our FIN.
        #      => He'll get a RST. We don't care.
        #
        #   2. Server has not closed the connection on the application layer,
        #      because it was impossible (network error, client error).
        #      => It is *not* the server's fault.
        #
        #   3. Server has not closed the connection on the application layer,
        #      but *should* have closed it.
        #      => It *is* the server's fault, but this class does not care.
        #         HTTP semantics is caller's business.

        # Also: a TIME_WAIT period (as consequence of an active close) is OK.

        try:
            # For non-socket `HalfCloseableStream`s, aclose() may block,
            # but always closes the underlying resource before returning.
            with trio.move_on_after(self.shutdown_timeout): # Hence the timeout.
                await self.stream.aclose()
        except trio.ClosedResourceError:
            pass  # ensures idempotency

    def basic_headers(self) -> List[Tuple[bytes, bytes]]:  # h11._headers.Headers
        # HTTP requires these headers in all responses
        return [
            (b"Date", format_date_time(None).encode("ascii")),
            (b"Server", self.server_header),
        ]

    def info(self, msg: str) -> None:
        print(f"{self._connection_id}: {msg}")

    async def send_error(self, status_code: int, msg: str) -> None:
        """
        Send a JSON error message if possible.
        Otherwise, shut down the connection.
        """

        if self.conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
            # Cannot send an error; we can only terminate the connection.
            await self.ensure_shutdown()
            return

        json_msg = json.dumps({"error": msg})
        body = json_msg.encode("utf-8")
        #self.info("Error response: " + json_msg)

        headers = self.basic_headers() + [
            (b"Content-Length", b"%d" % len(body)),
            (b"Content-Type", b"application/json"),
        ]

        await self.send(h11.Response(status_code=status_code, headers=headers))
        await self.send(h11.Data(data=body))
        await self.send(h11.EndOfMessage())

################################################################
