import trio, h11, threading
from ._adapter import TrioHTTPConnection
from ._config import parse_host_and_port
from functools import partial
from typing import Callable, Union, Iterable, Tuple

################################################################
#                  The proxy itself
################################################################

async def handle(stream: trio.SocketStream, is_allowed: Callable[[str, int], bool]) -> None:
    """
    Handles one HTTP CONNECT request from start to end, allowing only whitelisted connections.

    `is_allowed` must take hostname (str) and port (int).
    """
    start_time = trio.current_time()
    w = TrioHTTPConnection(stream, shutdown_timeout=10)

    try:
        assert w.conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}

        REQUEST_TIMEOUT = 5
        CONNECTION_TIMEOUT = 5
        client_request_completed = False  # to distinguish between the two timeouts

        with trio.fail_after(REQUEST_TIMEOUT):
            # Regular event sequence:
            # -----------------------
            #   1. Request (= start of request)
            #   2. Data* (optional)
            #   3. EndOfMessage (= end of request)
            #
            # At any moment: ConnectionClosed or exception
            e = await w.next_event()
            assert isinstance(e, (h11.Request, h11.ConnectionClosed)), "This assertion should always hold"

            if isinstance(e, h11.ConnectionClosed):
                w.info("Client closed the TCP connection")
                return

            if e.method != b"CONNECT":
                await w.send_error(405, f"Method {e.method!r} is not allowed")
                return

            # Ignore any HTTP body (h11.Data entries)
            # and read until h11.EndOfMessage
            while type(await w.next_event()) is not h11.EndOfMessage:
                pass

            target_host = e.target.decode("ascii")  # h11 ensures that this cannot break

        try:
            host, port = parse_host_and_port(target_host)
        except ValueError:
            await w.send_error(400, f"Malformed hostname: {target_host!r}")
            return

        client_request_completed = True

        if not is_allowed(host, port):
            await w.send_error(403, f"Host/port combination not allowed: {host}:{port}")
            return

        w.info(f"Making TCP connection to {host}:{port}")

        with trio.fail_after(CONNECTION_TIMEOUT):
            try:
                target_stream = await trio.open_tcp_stream(host, int(port))  # takes _exactly_ int
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
        w.info(f"Handling exception: {e!r}")
        try:
            if isinstance(e, trio.BrokenResourceError):
                w.info("Client abruptly closed connection; dropping request.")
            elif isinstance(e, h11.RemoteProtocolError):
                await w.send_error(e.error_status_hint, str(e))
            elif isinstance(e, trio.TooSlowError):
                if not client_request_completed:
                    await w.send_error(408, "Client is too slow, terminating connection")
                else:
                    await w.send_error(504, "TCP connection to server timed out")
            else:
                w.info(f"Internal Server Error: {type(e)} {e}")
                await w.send_error(500, str(e))
            await w.ensure_shutdown()
        except Exception as e:
            import traceback
            w.info("Error while responding with 500 Internal Server Error: " + "\n".join(traceback.format_tb(e.__traceback__)))
            await w.ensure_shutdown()
    finally:
        end_time = trio.current_time()
        w.info(f"Total time: {end_time - start_time:.6f}s")


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

################################################################
#                  User-friendly objects
################################################################

Whitelist = Iterable[Tuple[str, int]]
Predicate = Callable[[str, int], bool]


class TunnelProxy:
    """
    An HTTP CONNECT proxy which only allows whitelisted connections.

    Runs on a trio event loop.
    """

    def __init__(self, allowed_hosts: Union[Whitelist, Predicate]):
        """
        `allowed_hosts` is either a list of (host, port) pairs,
        or a predicate function taking a host and a port.
        """
        self.update(allowed_hosts)

    async def listen(self, host: str, port: int) -> None:
        """
        Listen for incoming TCP connections.

        Parameters:
          host: the host interface to listen on
          port: the port to listen on
        """
        print(f"Listening on http://{host}:{port}")
        h = partial(handle, is_allowed=self.is_allowed)
        await trio.serve_tcp(h, port, host=host)

    def update(self, allowed_hosts: Union[Whitelist, Predicate]) -> None:
        """
        Update the whitelist.

        `allowed_hosts` is either a list of (host, port) pairs,
        or a predicate function taking a host and a port.
        """
        if callable(allowed_hosts):
            self.is_allowed = allowed_hosts
        else:
            self.is_allowed = lambda host, port: (host, port) in set(allowed_hosts)


async def run_synchronously_cancellable_proxy(
        proxy: TunnelProxy,
        host: str,
        port: int,
        stop: threading.Event,
        stop_check_interval: float,
    ) -> None:
    """
    Runs the proxy, until cancelled through the `stop` event.
    It checks the event every `stop_check_interval` seconds.

    This function is meant for use primarily in the synchronous world:
    while it _can_ be used just fine in Trio, a plain trio.CancelScope
    is simpler and more idiomatic.
    """

    async def listen_for_stop(cancel_scope: trio.CancelScope) -> None:
        while not stop.is_set():
            await trio.sleep(stop_check_interval)
        cancel_scope.cancel()

    async with trio.open_nursery() as nursery:
        nursery.start_soon(listen_for_stop, nursery.cancel_scope)
        nursery.start_soon(proxy.listen, host, port)


class SynchronousTunnelProxy:
    """
    A wrapper around TunnelProxy which runs it in a separate
    thread, so you can use it from a traditional threaded program.

    Can stop, but not gently. (It kills all TCP connections.)
    """

    def __init__(self,
            host: str,
            port: int,
            allowed_hosts: Union[Whitelist, Predicate],
            stop_check_interval: float = 0.010,
            ):
        """
        Parameters are similar to TunnelProxy. `stop_check_interval` is new:
        this is how long (in seconds) it may take to stop the proxy.
        """
        self._proxy = TunnelProxy(allowed_hosts)
        self._started = False
        self._stop = threading.Event()

        self._thread = threading.Thread(
            name=f"SynchronousTunnelProxy-on-http://{host}:{port}/",
            target=trio.run,
            args=(run_synchronously_cancellable_proxy, self._proxy, host, port, self._stop, stop_check_interval),
        )

    def start(self) -> None:
        """Start the proxy, if not already started."""
        if not self._started:
            self._thread.start()
            self._started = True

    def stop(self) -> None:
        """Stop the proxy, if not already stopped."""
        self._stop.set()
        self._thread.join()
