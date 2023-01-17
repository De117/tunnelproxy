# A small whitelisting HTTP CONNECT proxy
This is a small HTTP CONNECT proxy, with a target host whitelist.

Implemented in Python on top of [trio][1] and [h11][2], it is written for ease
of comprehension and auditing. (This makes it easy to adopt in situations where
you'd want such a proxy.)

A secondary goal is to be flexible. It can be used in two ways:

  1. As a stand-alone proxy.
     Just run the module:
     ```sh
     python -m tunnelproxy --address localhost --port 8080 --config example-config.json
     ```

  2. As a library.
     The proxy (`TunnelProxy`) always runs in Trio's event loop, but a
     wrapper (`SynchronousTunnelProxy`) lets you run it from normal code.

     Make it your own!

For example of (2), see `tunnelproxy/__main__.py`.


# Performance
The proxy is single-threaded. On an Intel i7-7700HQ @ 2.80GHz, it handles ~560
connections per second. Not much, but enough for many use cases.


# License
This project is [MIT licensed][3]. `TrioHTTPConnection` from `adapter.py` is
based on [h11's example server][4], by Nathaniel J. Smith. The rest is written
by Antun Maldini.


[1]: https://github.com/python-trio/trio#readme
[2]: https://github.com/python-hyper/h11#readme
[3]: https://mit-license.org/
[4]: https://github.com/python-hyper/h11/blob/v0.14.0/examples/trio-server.py
