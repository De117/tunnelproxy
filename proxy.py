import trio, h11, threading
from adapter import TrioHTTPConnection
from functools import partial
from typing import Callable, Union, Iterable, Tuple


async def handle(stream: trio.SocketStream, is_whitelisted: Callable[[str, int], bool]) -> None:
    """
    Handles one HTTP CONNECT request from start to end, allowing only whitelisted connections.

    `is_whitelisted` must take hostname (str) and port (int).
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

            target_host = e.target

        number_of_colons = target_host.count(b":")
        if number_of_colons > 1:
            await w.send_error(400, f"Malformed hostname: {target_host!r}")
            return
        elif number_of_colons == 1:
            host_bytes, port_bytes = target_host.split(b":")
            try:
                port = int(port_bytes)
                assert port in range(65536)
            except (ValueError, AssertionError):
                await w.send_error(400, f"Invalid port number: {port_bytes!r}")
                return
        else:
            host_bytes, port = target_host, 80

        host = host_bytes.decode("ascii")  # h11 ensures that this cannot break

        client_request_completed = True

        if not is_whitelisted(host, port):
            await w.send_error(403, f"Host/port combination not allowed: {host}:{port}")
            return

        w.info(f"Making TCP connection to {host}:{port}")

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
        w.info(f"Handling exception: {e!r}")
        try:
            if isinstance(e, h11.RemoteProtocolError):
                await w.send_error(e.error_status_hint, str(e))
            elif isinstance(e, trio.TooSlowError):
                if not client_request_completed:
                    await w.send_error(408, "Client is too slow, terminating connection")
                else:
                    await w.send_error(504, "TCP connection to server timed out")
            else:
                w.info(f"Internal Server Error: {type(e)} {e}")
                await w.send_error(500, str(e))
            await w.shutdown_and_clean_up()
        except Exception as e:
            import traceback
            w.info("Error while responding with 500 Internal Server Error: " + "\n".join(traceback.format_tb(e.__traceback__)))
            await w.shutdown_and_clean_up()
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


class WhitelistingProxy:
    """
    An HTTP forwards proxy which only allows connections from a whitelist.

    Runs on a trio event loop.
    """

    def __init__(self, whitelist: Union[Iterable[Tuple[str, int]], Callable[[str, int], bool]]):
        """
        `whitelist` is either a list of (host, port) pairs,
        or a predicate function taking a host and a port.
        """
        if not callable(whitelist):
            self.is_whitelisted = lambda host, port: (host, port) in set(whitelist)
        else:
            self.is_whitelisted = whitelist

    async def listen(self, host: str, port: int) -> None:
        """
        Listen for incoming TCP connections.

        Parameters:
          host: the host interface to listen on
          port: the port to listen on
        """
        print(f"Listening on http://{host}:{port}")
        h = partial(handle, is_whitelisted=self.is_whitelisted)
        await trio.serve_tcp(h, port, host=host)


class SynchronousWhitelistingProxy:
    """
    A wrapper around WhitelistingProxy which runs it in a separate
    thread, so you can use it from a traditional threaded program.

    Can stop, but not gently. (It kills all TCP connections.)
    """

    def __init__(self,
            host: str,
            port: int,
            whitelist: Union[Iterable[Tuple[str, int]], Callable[[str, int], bool]],
            stop_check_interval: float = 0.010,
            ):
        """
        Parameters are similar to WhitelistingProxy. `stop_check_interval` is new:
        this is how long (in seconds) it may take to stop the proxy.
        """
        self._proxy = WhitelistingProxy(is_whitelisted)
        self._started = False
        self._stop = threading.Event()

        async def runner(proxy: WhitelistingProxy, stop: threading.Event) -> None:

            async def listen_for_stop(cancel_scope: trio.CancelScope) -> None:
                while not stop.is_set():
                    await trio.sleep(stop_check_interval)
                cancel_scope.cancel()

            async with trio.open_nursery() as nursery:
                nursery.start_soon(listen_for_stop, nursery.cancel_scope)
                nursery.start_soon(proxy.listen, host, port)

        self._thread = threading.Thread(
            name=f"SynchronousWhitelistingProxy-on-http://{host}:{port}/",
            target=trio.run,
            args=(runner, self._proxy, self._stop),
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


def is_whitelisted(host: str, port: int) -> bool:
    whitelist = [
        ("example.com", 80),
        ("example.com", 443),
        ("www.example.com", 80),
        ("www.example.com", 443),
        ("localhost", 1234),
        ("localhost", 12345),
    ]
    return (host, port) in whitelist


if __name__ == "__main__":
    try:
        # On trio (async)
        #proxy = WhitelistingProxy(is_whitelisted)
        #trio.run(proxy.listen, "0.0.0.0", 8080)

        # With wrapper (sync)
        proxy = SynchronousWhitelistingProxy("localhost", 8080, is_whitelisted)
        proxy.start()
        import time; time.sleep(5)
        proxy.stop()
    except KeyboardInterrupt:
        print("KeyboardInterrupt - shutting down")
