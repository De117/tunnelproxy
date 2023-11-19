use tokio::net::TcpStream;
use tokio::io::AsyncWriteExt;
//use tokio::task::{JoinHandle, JoinError};
use std::io::{Read, Write};
use std::time::{Instant, Duration};

// const HTTP_HOST: &'static str = "localhost:8080";
const TARGET_IP: &'static str = "127.0.0.1";
const TARGET_PORT: u16 = 2222;
const PROXY_IP: &'static str = "127.0.0.1";
const PROXY_PORT: u16 = 8080;
const BUFFER_SIZE: usize = 1000;

#[derive(Debug, Clone, Copy)]
struct Sample {
    /// Time to establish TCP connection
    t_connect: Duration,
    /// Time to send our message
    t_send: Duration,
    /// Total time (until we've read the result)
    t_total: Duration,
    /// Was the sample successful (as opposed to an error)?
    ok: bool,
}

fn average(xs: &Vec<f64>) -> f64 {
    let mut total: f64 = 0.0;
    for x in xs {
        total += x;
    }
    let n = xs.len() as f64;
    return total / n;
}

fn stddev(xs: &Vec<f64>) -> f64 {
    let mean = average(xs);
    let mut total = 0.0;
    for x in xs {
        total += (x - mean) * (x - mean);
    }
    let n = xs.len() as f64;
    return (total / n).sqrt();
}

fn spin_until(t: Instant) -> Duration {
    loop {
        let now = Instant::now();
        let dt = now - t;
        if dt > Duration::ZERO {
            return dt;
        }
    }
}

fn make_request(request_id: &str, _ip_address: &str, _tcp_port: u16) -> Sample {
    let proxy_request = format!("CONNECT localhost:{TARGET_PORT} HTTP/1.1\r\nHost: {PROXY_IP}:{PROXY_PORT}\r\n\r\n").into_bytes();
    let http_request = format!("GET / HTTP/1.1\r\nHost: {TARGET_IP}:{TARGET_PORT}\r\n\r\n").into_bytes();

    let mut buffer = [0u8; BUFFER_SIZE];

    let log = |_s: &str| -> () {println!("Request {}: {}", request_id, _s)};

    let t0 = Instant::now();

    let mut t_connect = None;
    let mut t_send = None;
    let mut t_read = None;
    let ok: bool;

    match std::net::TcpStream::connect((PROXY_IP, PROXY_PORT)) {
        Err(_) => {ok = false; log("Could not connect")}
        Ok(mut stream) => {
            t_connect = Some(Instant::now());
            match stream.write_all(proxy_request.as_slice()) {
                Err(_) => {ok = false; log("Could not write")}
                Ok(_) => {
                    t_send = Some(Instant::now());
                    match stream.read(&mut buffer) {
                        Err(_) => {ok = false; log("Could not read")}
                        Ok(_n) => {
                            t_read = Some(Instant::now());
                            //log(format!("Read {} bytes: {:?}", _n, std::str::from_utf8(&buffer[.._n])).as_str());
                            match stream.write_all(http_request.as_slice()) {
                                Err(_) => {ok = false; log("Could not write 2")}
                                Ok(_) => {
                                    match stream.read(&mut buffer) {
                                        Err(_) => {ok = false; log("Could not read 2")}
                                        Ok(_n) => {
                                            //log(format!("Read {} bytes: {:?}", _n, std::str::from_utf8(&buffer[.._n])).as_str());
                                            ok = true;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    let s = Sample {
        t_connect: t_connect.unwrap_or(t0) - t0,
        t_send:    t_send.unwrap_or(t0) - t_connect.unwrap_or(t0),
        t_total:   t_read.unwrap_or(t0) - t0,
        ok,
    };
    return s
}

async fn _test_one_tokio(request_id: &str, ip_address: &str, tcp_port: u16) -> Sample {
    let http_request = b"GET / HTTP/1.1\r\nHost: localhost:8080\r\n\r\n";

    let mut buffer = [0u8; BUFFER_SIZE];

    let log = |_s: &str| -> () {println!("Request {}: {}", request_id, _s)};

    let t0 = Instant::now();

    let mut t_connect = None;
    let mut t_send = None;
    let mut t_read = None;
    let ok: bool;

    match tokio::net::TcpStream::connect((ip_address, tcp_port)).await {
        Err(_) => {ok = false; log("Could not connect")}
        Ok(mut stream) => {
            t_connect = Some(Instant::now());
            match stream.write_all(http_request).await {
                Err(_) => {ok = false; log("Could not write")}
                Ok(_) => {
                    t_send = Some(Instant::now());
                    _ = stream.readable().await;
                    match stream.try_read(&mut buffer) {
                        Err(_) => {ok = false; log("Could not read")}
                        Ok(_n) => {
                            t_read = Some(Instant::now());
                            //log(format!("Read {} bytes: {:?}", _n, std::str::from_utf8(&buffer[..n])).as_str());
                            ok = true;
                        }
                    }
                }
            }
        }
    }
    let s = Sample {
        t_connect: t_connect.unwrap_or(t0) - t0,
        t_send:    t_send.unwrap_or(t0) - t_connect.unwrap_or(t0),
        t_total:   t_read.unwrap_or(t0) - t0,
        ok,
    };
    return s
}

async fn _test(worker_id: u32, num_requests: u32, ip_address: &str, tcp_port: u16, requests_per_second: u32) -> Vec<Sample> {
    println!("Worker {}, making {} requests to {}:{}", worker_id, num_requests, ip_address, tcp_port);

    // let http_request = format!("GET / HTTP/1.1\r\nHost: {}\r\n\r\n", HTTP_HOST).as_bytes();
    let http_request = b"GET / HTTP/1.1\r\nHost: localhost:8080\r\n\r\n";

    let mut buffer = [0u8; BUFFER_SIZE];

    let period = 1.0 / requests_per_second as f64;
    println!("Period: {} Hz", period);

    let mut samples = Vec::with_capacity(num_requests as usize);

    for _i in 0..num_requests {
        let log = |_s: &str| -> () {println!("Worker {}, request {}: {}", worker_id, _i, _s)};

        let t0 = Instant::now();

        let mut t_connect = None;
        let mut t_send = None;
        let mut t_read = None;
        let ok: bool;

        match TcpStream::connect((ip_address, tcp_port)).await {
            Err(_) => {ok = false; log("Could not connect")}
            Ok(mut stream) => {
                t_connect = Some(Instant::now());
                match stream.write_all(http_request).await {
                    Err(_) => {ok = false; log("Could not write")}
                    Ok(_) => {
                        t_send = Some(Instant::now());
                        _ = stream.readable().await;
                        match stream.try_read(&mut buffer) {
                            Err(_) => {ok = false; log("Could not read")}
                            Ok(_n) => {
                                t_read = Some(Instant::now());
                                //log(format!("Read {} bytes: {:?}", _n, std::str::from_utf8(&buffer[..n])).as_str());
                                ok = true;
                            }
                        }
                    }
                }
            }
        }
        let s = Sample {
            t_connect: t_connect.unwrap_or(t0) - t0,
            t_send:    t_send.unwrap_or(t0) - t_connect.unwrap_or(t0),
            t_total:   t_read.unwrap_or(t0) - t0,
            ok,
        };
        samples.push(s);
    }
    return samples;
}

fn print_latencies(durations: &Vec<Duration>) {
    let num_measurements = durations.len();
    let mut latencies = durations.clone();
    latencies.sort();
    println!("Latency percentiles:");
    println!("  min    - {:?}", latencies[num_measurements *  0 / 100]);
    println!("  1      - {:?}", latencies[num_measurements *  1 / 100]);
    println!("  10     - {:?}", latencies[num_measurements * 10 / 100]);
    println!("  50     - {:?}", latencies[num_measurements * 50 / 100]);
    println!("  99     - {:?}", latencies[num_measurements * 99 / 100]);
    println!("  99.9   - {:?}", latencies[num_measurements * 999 / 1_000]);
    println!("  99.99  - {:?}", latencies[num_measurements * 9999 / 10_000]);
    println!("  99.999 - {:?}", latencies[num_measurements * 99999 / 100_000]);
    println!("  max    - {:?}", latencies[num_measurements-1]);

    let mean = latencies.iter().sum::<Duration>() / latencies.len() as u32;
    println!("Mean: {mean:?}");
}

//fn print_type_of<T>(_: &T) {println!("{}", std::any::type_name::<T>())}

fn run_thread(initial_request_id: u64, num_requests: u64, frequency: u32, target_ip: &str, target_port: u16) -> (Vec<Sample>, Vec<Duration>) {
    println!("Starting thread, requests {initial_request_id}-{} at {frequency}/s", initial_request_id + num_requests);

    let period = Duration::from_nanos(1_000_000_000 / frequency as u64);
    let mut actual_times = Vec::<Duration>::with_capacity(num_requests as usize);
    let mut samples = Vec::<Sample>::with_capacity(num_requests as usize);

    let mut scheduled_at = Instant::now();
    for i in 0..num_requests {
        let sample = make_request(&format!("{}", initial_request_id + i), target_ip, target_port);
        actual_times.push(Instant::now() - scheduled_at);
        samples.push(sample);
        scheduled_at += period;
        spin_until(scheduled_at);
    }
    return (samples, actual_times);
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> () {
    const NUM_REQUESTS: u64 = 100_000;

    let frequency = 1_000; // Hz
    println!("Running at frequency of {frequency} Hz");


    // At low rates & low latencies, we can do everything from a single thread.
    // At high rates or high latencies, our thread would get more and more delayed,
    // so we need to use multiple threads.
    // For _our_ testing, on localhost, 10kHz should be doable, but not much more.
    const FREQUENCY_TRESHOLD: u32 = 10_000;

    // Divide the work over threads as follows:
    //   1. frequency first, by filling (n-1) full blocks and 1 potentially empty block
    //   2. requests ∝ frequency, with factor total_requests / total_frequency
    let n = frequency as f64 / FREQUENCY_TRESHOLD as f64;

    let frequency_per_chunk = FREQUENCY_TRESHOLD;
    let requests_per_chunk = (NUM_REQUESTS as f64 / n).floor() as u64;

    let frequency_in_last_chunk: u32;
    let requests_in_last_chunk: u64;
    if frequency % FREQUENCY_TRESHOLD != 0 { // frequency doesn't cleanly divide into chunks
        frequency_in_last_chunk = frequency - n.floor() as u32 * frequency_per_chunk;
        requests_in_last_chunk = NUM_REQUESTS - n.floor() as u64 * requests_per_chunk;
    } else {
        frequency_in_last_chunk = frequency_per_chunk;
        requests_in_last_chunk = requests_per_chunk;
    }
    let num_threads = n.ceil() as u32;
    println!("num_threads: {num_threads}");

    let mut join_handles = Vec::<std::thread::JoinHandle<_>>::new();
    let t0 = Instant::now();
    for i in 0..num_threads {
        if i < num_threads - 1 {
            join_handles.push(std::thread::spawn(move ||
                run_thread(
                    i as u64 * requests_per_chunk,
                    requests_per_chunk,
                    frequency_per_chunk,
                    TARGET_IP,
                    TARGET_PORT
            )));
        } else {
            join_handles.push(std::thread::spawn(move ||
                run_thread(
                    i as u64 * requests_per_chunk,
                    requests_in_last_chunk,
                    frequency_in_last_chunk,
                    TARGET_IP,
                    TARGET_PORT
            )));
        }
    }
    let results = join_handles.into_iter().map(|h| h.join()).collect::<Vec<_>>();
    let t1 = Instant::now();

    let mut samples = Vec::<Sample>::new();
    let mut actual_times = Vec::<Duration>::new();
    for (i, r) in results.iter().enumerate() {
        match r {
            Err(e) => println!("Thread {i} failed with err: {:?}", e),
            Ok((s, t)) => {
                samples.extend(s);
                actual_times.extend(t);
            }
        }
    }

    let num_errors = samples.iter().filter(|s| !s.ok).count();
    let num_ok = samples.len() - num_errors;
    samples = samples.into_iter().filter(|s| s.ok).collect::<Vec<Sample>>();

    println!("{num_ok} OK requests, {num_errors} errors");

    print_latencies(&actual_times);

    let ts_connect: Vec<f64> = samples.iter().map(|s| -> f64 {s.t_connect.as_secs_f64()}).collect();
    let ts_send   : Vec<f64> = samples.iter().map(|s| -> f64 {s.t_send   .as_secs_f64()}).collect();
    let ts_total  : Vec<f64> = samples.iter().map(|s| -> f64 {s.t_total  .as_secs_f64()}).collect();

    let t_connect_avg: f64 = average(&ts_connect);
    let t_send_avg   : f64 = average(&ts_send);
    let t_total_avg  : f64 = average(&ts_total);

    let t_connect_stddev: f64 = stddev(&ts_connect);
    let t_send_stddev   : f64 = stddev(&ts_send);
    let t_total_stddev  : f64 = stddev(&ts_total);

    let t_connect_max: f64 = ts_connect.iter().fold(0.0, |acc, x| -> f64 {acc.max(*x)});
    let t_send_max   : f64 = ts_send   .iter().fold(0.0, |acc, x| -> f64 {acc.max(*x)});
    let t_total_max  : f64 = ts_total  .iter().fold(0.0, |acc, x| -> f64 {acc.max(*x)});

    let t_connect_min: f64 = ts_connect.iter().fold(f64::INFINITY, |acc, x| -> f64 {acc.min(*x)});
    let t_send_min   : f64 = ts_send   .iter().fold(f64::INFINITY, |acc, x| -> f64 {acc.min(*x)});
    let t_total_min  : f64 = ts_total  .iter().fold(f64::INFINITY, |acc, x| -> f64 {acc.min(*x)});

    println!("Average times:");
    println!("Connect: {:10.3} μs (σ {:10.3} μs, max {:10.3} μs)", 1e6 * t_connect_avg, 1e6 * t_connect_stddev, 1e6 * t_connect_max);
    println!("Send:    {:10.3} μs (σ {:10.3} μs, max {:10.3} μs)", 1e6 * t_send_avg,    1e6 * t_send_stddev   , 1e6 * t_send_max   );
    println!("Total:   {:10.3} μs (σ {:10.3} μs, max {:10.3} μs)", 1e6 * t_total_avg,   1e6 * t_total_stddev  , 1e6 * t_total_max  );
    println!();
    println!("Max. and min. times:");
    println!("Connect: max {:10.3} μs, min {:10.3} μs", 1e6 * t_connect_max, 1e6 * t_connect_min);
    println!("Send:    max {:10.3} μs, min {:10.3} μs", 1e6 * t_send_max   , 1e6 * t_send_min   );
    println!("Total:   max {:10.3} μs, min {:10.3} μs", 1e6 * t_total_max  , 1e6 * t_total_min  );

    println!("Total time elapsed: {:?}, {} μs per request", t1 - t0, 1e6 * (t1 - t0).as_secs_f64() / NUM_REQUESTS as f64);
}
