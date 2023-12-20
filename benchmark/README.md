# Benchmarking

This directory contains a simple HTTP client and server programs, both written
in Rust, to benchmark the proxy.

To run it:
```sh
# In a shell for the server: (in ./server-rust)
cargo build
ulimit -n $(ulimit -Hn)
target/release/server-rust

# In a shell for the proxy (in repository root)
ulimit -n $(ulimit -Hn)
echo '{"version":1,"allowed_hosts":["localhost:2222"]}' > config.json
python3 -m tunnelproxy --configuration-file config.json --address localhost --port 8080 > out.log

# In a shell for the client in (./client-rust)
cargo build --release
ulimit -n $(ulimit -Hn)
target/release/client-rust --proxy localhost:8080 --time 10 --frequency 500
```

The client will print statistics, with and without correction for coordinated
omission.


## Coordinated omission: what is it, and what to do about it?

Coordinated omission, [named by Gil Tene][1], is a problem that happens in the
usual way of measuring server latency. "The usual way" is: a client makes requests
requests to a server, measures how long each one takes individually, and calculates
the latency distribution. The problem with this is that slow requests can hold up
the very start of following requests, resulting in a lower rate, and _this is not
measured_.

To illustrate: send 100 requests, _sequentially_, at a rate of 100Hz. The server
stutters, and the first request takes 990ms, while the remaining ones all take 0.1ms.

The naive calculation gives a 99%-ile of 0.1ms and an average of ≈10ms, but
this ignores the fact that the requests were held up for half a second on average.

Had the client been requesting at a _constant rate_ of 100Hz (=a 10ms period),
i.e. with each request made independently and on time, we would see:
  * request 1 took 990ms from scheduled time to finish
  * request 2 took 979.9ms from scheduled time to finish
  * request 3 took 969.8ms from scheduled time to finish
  * etc.

The 99%-ile would be 979.9ms, and the average 499.95ms.

Now, with request 1 waiting, the only way to know whether request 2 will finish
quickly or have to wait due to a server hiccup, is to _make_ request 2 on time.

But if you assume it was a server hiccup, you can calculate the total time
request 2 took based on when it should have been sent. It's not perfect, but
it gives a reasonably good (and conservative) measurement of performance.

Giving the client multiple threads makes it a bit closer to the ideal case.


[1]: https://groups.google.com/g/mechanical-sympathy/c/icNZJejUHfE/m/BfDekfBEs_sJ
