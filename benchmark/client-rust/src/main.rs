use std::io::{Read, Write};
use std::time::{Instant, Duration};
use argparse::{ArgumentParser, Store, StoreOption};
use std::fmt::{Display, Formatter};

#[derive(Clone)]
struct Host {
    hostname: String,
    port: u16,
}
impl Display for Host {
    fn fmt(&self, f: &mut Formatter) -> std::fmt::Result {
        write!(f, "{}:{}", self.hostname, self.port)
    }
}

fn parse_host(s: &str) -> Option<Host> {
    if let Some((hostname, port_string)) = s.split_once(':') {
       let port: u16 = port_string.parse().unwrap();
       let hostname = String::from(hostname);
       return Some(Host {hostname, port})
    }
    None
}
const BUFFER_SIZE: usize = 1000;

#[derive(Debug, Clone, Copy)]
struct Sample {
    /// Time to establish TCP connection
    t_connect: Duration,
    /// Time to get a response from the proxy (if used)
    t_proxy: Duration,
    /// Time until we've read the result from the target server
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

/// Make a request to `target`, optionally going through `proxy` using CONNECT.
///
/// Returns a sample: how did things go for this particular request?
fn make_request(proxy: Option<&Host>, target: &Host) -> Sample {
    let proxy_request = proxy.map(|p| format!("CONNECT {} HTTP/1.1\r\nHost: {}\r\n\r\n", target, p).into_bytes());
    let http_request = format!("GET / HTTP/1.1\r\nHost: {}\r\n\r\n", target).into_bytes();

    let mut buffer = [0u8; BUFFER_SIZE];

    let t0 = Instant::now();

    let mut t_connect = None;
    let mut t_proxy = None;
    let mut t_total = None;

    let ok: bool = (|| {
        let server = proxy.unwrap_or(target);

        let mut stream = std::net::TcpStream::connect((&server.hostname as &str, server.port))?;
        t_connect = Some(Instant::now());

        if proxy.is_some() {
            stream.write_all(proxy_request.unwrap().as_slice())?;
            let _n = stream.read(&mut buffer)?;
            t_proxy = Some(Instant::now());
        }
        stream.write_all(http_request.as_slice())?;
        let _n = stream.read(&mut buffer)?;
        t_total = Some(Instant::now());

        Ok(true) as Result<bool, std::io::Error>
    })().unwrap_or(false);

    let s = Sample {
        t_connect: t_connect.unwrap_or(t0) - t0,
        t_proxy:   t_proxy.unwrap_or(t0) - t_connect.unwrap_or(t0),
        t_total:   t_total.unwrap_or(t0) - t0,
        ok,
    };
    return s
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

fn run_thread(initial_request_index: u64, num_requests: u64, frequency: u32, proxy: Option<&Host>, target: &Host) -> (Vec<Sample>, Vec<Duration>) {
    println!("Starting thread, requests {initial_request_index}-{} at {frequency}/s", initial_request_index + num_requests);

    let period = Duration::from_nanos(1_000_000_000 / frequency as u64);
    let mut actual_times = Vec::<Duration>::with_capacity(num_requests as usize);
    let mut samples = Vec::<Sample>::with_capacity(num_requests as usize);

    let mut scheduled_at = Instant::now();
    for _ in 0..num_requests {
        let sample = make_request(proxy, target);
        actual_times.push(Instant::now() - scheduled_at);
        samples.push(sample);
        scheduled_at += period;
        spin_until(scheduled_at);
    }
    return (samples, actual_times);
}

/// The load of a single OS thread.
#[derive(Copy, Clone)]
struct Chunk {
    frequency: u32,
    num_requests: u64,
}

/// At low rates & low latencies, we can do everything from a single thread.
/// At high rates or high latencies, one thread would get more and more delayed,
/// so we need to use multiple threads.
///
/// This function calculates the division of work over n threads, such that all
/// threads except maybe the last one run at maximum frequency, and a thread's
/// number of requests proportional to its frequency.
///
/// Returns: (n, normal_chunk, last_chunk)
fn calculate_chunks(max_frequency_per_thread: u32, frequency: u32, num_requests: u64) -> (u32, Chunk, Chunk) {

    // Divide the work over threads as follows:
    //   1. frequency first, by filling (n-1) full blocks and 1 potentially empty block
    //   2. requests ∝ frequency, with factor total_requests / total_frequency
    let n = frequency as f64 / max_frequency_per_thread as f64;

    let frequency_per_chunk = max_frequency_per_thread;
    let requests_per_chunk = (num_requests as f64 / n).floor() as u64;

    let frequency_in_last_chunk: u32;
    let requests_in_last_chunk: u64;
    if frequency % max_frequency_per_thread != 0 {
        // frequency doesn't cleanly divide into chunks
        frequency_in_last_chunk = frequency - n.floor() as u32 * frequency_per_chunk;
        requests_in_last_chunk = num_requests - n.floor() as u64 * requests_per_chunk;
    } else {
        frequency_in_last_chunk = frequency_per_chunk;
        requests_in_last_chunk = requests_per_chunk;
    }
    let num_threads = n.ceil() as u32;

    return (
        num_threads,
        Chunk{frequency: frequency_per_chunk, num_requests: requests_per_chunk},
        Chunk{frequency: frequency_in_last_chunk, num_requests: requests_in_last_chunk},
    );
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> () {

    let mut frequency: u32 = 1_000; // Hz
    let mut maybe_num_requests: Option<u64> = None;
    let mut maybe_time: Option<u32> = None; // seconds
    let mut maybe_proxy_string: Option<String> = None;
    let mut target_string: String = "localhost:2222".to_owned();

    {
        let mut parser = ArgumentParser::new();
        parser.refer(&mut maybe_num_requests).add_option(&["-n", "--num-requests"], StoreOption, "Number of requests to send");
        parser.refer(&mut frequency         ).add_option(&["-f", "--frequency"], Store, "Desired send rate (in Hz)");
        parser.refer(&mut maybe_time        ).add_option(&["--time"], StoreOption, "How long to send requests (in s)");
        parser.refer(&mut maybe_proxy_string).add_option(&["--proxy"], StoreOption, "Which HTTP CONNECT proxy to use, if any; e.g. localhost:12345");
        parser.refer(&mut target_string     ).add_option(&["--target"], Store, "Which target to hit; e.g. localhost:12345");
        parser.parse_args_or_exit();
    }

    let num_requests: u64;
    if maybe_time == None && maybe_num_requests == None {
        eprintln!(concat!(
            "Error parsing command line arguments: ",
            "Either time or number of requests must be specified!",
        ));
        std::process::exit(1);
    } else if maybe_time == None {
        num_requests = maybe_num_requests.unwrap();
    } else {
        num_requests = (maybe_time.unwrap() * frequency) as u64;
    }

    let proxy = maybe_proxy_string.map(|s| parse_host(&s).expect("Malformed proxy"));
    let target = parse_host(&target_string).expect("Malformed target");
    println!("Proxy: {}", proxy.clone().map_or(String::from("None"), |h| h.to_string()));
    println!("Target: {}", target);

    println!("Running at frequency of {frequency} Hz");

    // For _our_ testing, on localhost, 5kHz (=200μs per connection) should be doable.
    let (num_threads, normal_chunk, last_chunk) =
        calculate_chunks(5_000, frequency, num_requests);

    println!("num_threads: {num_threads}");

    let mut join_handles = Vec::<std::thread::JoinHandle<_>>::new();
    let t0 = Instant::now();
    for i in 0..num_threads {
        let chunk = if i < num_threads - 1 {normal_chunk} else {last_chunk};

        // copies to move into thread
        let proxy = proxy.clone();
        let target = target.clone();

        join_handles.push(std::thread::spawn(move ||
            run_thread(
                i as u64 * normal_chunk.num_requests,
                chunk.num_requests,
                chunk.frequency,
                proxy.as_ref(),
                &target,
        )));
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

    println!();
    println!("Corrected for coordinated omission, we have:");
    print_latencies(&actual_times);

    println!();
    println!("Without correcting for coordinated omission, we have:");
    let uncorrected_times: Vec<Duration> = samples.iter().map(|s| s.t_total).collect();
    print_latencies(&uncorrected_times);

    // Some more detailed statistics
    let ts_connect: Vec<f64> = samples.iter().map(|s| -> f64 {s.t_connect.as_secs_f64()}).collect();
    let ts_send   : Vec<f64> = samples.iter().map(|s| -> f64 {s.t_proxy  .as_secs_f64()}).collect();
    let ts_total  : Vec<f64> = samples.iter().map(|s| -> f64 {s.t_total  .as_secs_f64()}).collect();

    let t_connect_avg: f64 = average(&ts_connect);
    let t_proxy_avg  : f64 = average(&ts_send);
    let t_total_avg  : f64 = average(&ts_total);

    let t_connect_stddev: f64 = stddev(&ts_connect);
    let t_proxy_stddev  : f64 = stddev(&ts_send);
    let t_total_stddev  : f64 = stddev(&ts_total);

    let t_connect_max: f64 = ts_connect.iter().fold(0.0, |acc, x| -> f64 {acc.max(*x)});
    let t_proxy_max  : f64 = ts_send   .iter().fold(0.0, |acc, x| -> f64 {acc.max(*x)});
    let t_total_max  : f64 = ts_total  .iter().fold(0.0, |acc, x| -> f64 {acc.max(*x)});

    let t_connect_min: f64 = ts_connect.iter().fold(f64::INFINITY, |acc, x| -> f64 {acc.min(*x)});
    let t_proxy_min  : f64 = ts_send   .iter().fold(f64::INFINITY, |acc, x| -> f64 {acc.min(*x)});
    let t_total_min  : f64 = ts_total  .iter().fold(f64::INFINITY, |acc, x| -> f64 {acc.min(*x)});

    println!();
    println!("Per \"stage\" of a request:");
    println!("TCP connect: mean {:8.3} μs, σ {:8.3} μs, min {:8.3} μs, max {:8.3} μs", 1e6 * t_connect_avg, 1e6 * t_connect_stddev, 1e6 * t_connect_min, 1e6 * t_connect_max);
    println!("Proxy:       mean {:8.3} μs, σ {:8.3} μs, min {:8.3} μs, max {:8.3} μs", 1e6 * t_proxy_avg,   1e6 * t_proxy_stddev  , 1e6 * t_proxy_min  , 1e6 * t_proxy_max  );
    println!("Total:       mean {:8.3} μs, σ {:8.3} μs, min {:8.3} μs, max {:8.3} μs", 1e6 * t_total_avg,   1e6 * t_total_stddev  , 1e6 * t_total_min  , 1e6 * t_total_max  );

    println!();
    println!("Total time elapsed: {:?}, {} μs per request", t1 - t0, 1e6 * (t1 - t0).as_secs_f64() / num_requests as f64);
}
