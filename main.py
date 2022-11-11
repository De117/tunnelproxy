import trio, threading, signal, argparse, sys
from functools import partial
from typing import Union, Tuple, Set
from proxy import WhitelistingProxy, SynchronousWhitelistingProxy, load_configuration_from_file

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--configuration-file", required=True,
        help="Path to configuration file."
        'Format: {"version": 1, "allowed_hosts": ["foo.com:443", ...]}'
    )
    parser.add_argument("--address", default="localhost", help="IP address to listen on")
    parser.add_argument("--port", default=8080, type=int, help="TCP port to listen on")
    parser.add_argument("--proxy-interface", choices=("sync", "async"), default="async", help="Which internal interface to use")
    conf = parser.parse_args()
    if conf.port not in range(65536):
        parser.error("Value for --port is out of range (0-65535)")
    load_config = partial(load_configuration_from_file, conf.configuration_file)

    if conf.proxy_interface == "sync":
        CONFIGURATION = load_config()
        CONFIGURATION_LOCK = threading.Lock()

        def reload_config_sync(*_):
            try:
                x = load_config()
            except RuntimeError as e:
                print(e, file=sys.stderr)
            else:
                global CONFIGURATION, CONFIGURATION_LOCK
                with CONFIGURATION_LOCK:
                    CONFIGURATION = x
                print("Reloaded configuration", file=sys.stderr)

        signal.signal(signal.SIGHUP, reload_config_sync)

        def is_whitelisted(host: str, port: int) -> bool:
            with CONFIGURATION_LOCK:
                return (host, port) in CONFIGURATION.whitelist

        proxy = SynchronousWhitelistingProxy(conf.address, conf.port, is_whitelisted)
        proxy.start()

    else:
        CONFIGURATION = load_config()
        async def reload_config(proxy: WhitelistingProxy):
            with trio.open_signal_receiver(signal.SIGHUP) as received_signals:
                async for signal_num in received_signals:
                    try:
                        x = load_config()
                    except RuntimeError as e:
                        print(e, file=sys.stderr)
                    else:
                        proxy.update(x.whitelist)
                        print("Reloaded configuration", file=sys.stderr)

        async def main():
            proxy = WhitelistingProxy(CONFIGURATION.whitelist)
            async with trio.open_nursery() as nursery:
                nursery.start_soon(reload_config, proxy)
                nursery.start_soon(proxy.listen, conf.address, conf.port)
        try:
            trio.run(main)
        except KeyboardInterrupt:
            print("KeyboardInterrupt - shutting down")
