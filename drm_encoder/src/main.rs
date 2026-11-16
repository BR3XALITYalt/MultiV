// SPDX-License-Identifier: GPL-2.0-or-later

use decdrm_station::{SampleFormat, SignalFormat, Station, StationConfig};
use std::env;
use std::error::Error;
use std::io::{self, Write};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

fn main() {
    if let Err(error) = run() {
        eprintln!("DRM encoder: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let mut args = env::args_os().skip(1);
    let config_path = args.next().map(PathBuf::from).ok_or("missing station configuration path")?;
    let stop_path = args.next().map(PathBuf::from).ok_or("missing stop-file path")?;
    if args.next().is_some() {
        return Err("unexpected command-line argument".into());
    }

    let mut config = StationConfig::load(config_path)?;
    config.output.file = None;
    config.output.device = None;
    config.output.sample_format = SampleFormat::Float32;
    config.output.format = SignalFormat::Real;
    config.output.if_hz = Some(3_000.0);

    let mut station = Station::new(config)?;
    if station.output_channels() != 1 {
        return Err("the DRM bridge requires one real-IF output channel".into());
    }

    let stop = station.stop_handle();
    let watcher_stop = stop.clone();
    let watcher = thread::Builder::new()
        .name("txitpy-drm-stop-watch".into())
        .spawn(move || {
            while !watcher_stop.is_stopped() {
                if stop_path.exists() {
                    watcher_stop.stop();
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
        })?;

    let mut output = io::stdout().lock();
    let mut bytes = Vec::with_capacity(19_200 * 4);
    let frame_period = Duration::from_millis(400);
    let result = (|| -> Result<(), Box<dyn Error>> {
        while !station.inputs_finished() && !stop.is_stopped() {
            let frame_start = Instant::now();
            let samples = station.transmit_frame()?;
            bytes.clear();
            bytes.reserve(samples.len().saturating_mul(4));
            for sample in samples {
                bytes.extend_from_slice(&sample.to_le_bytes());
            }
            output.write_all(&bytes)?;
            output.flush()?;

            let remaining = frame_period.saturating_sub(frame_start.elapsed());
            if !remaining.is_zero() && !stop.is_stopped() {
                thread::sleep(remaining);
            }
        }
        Ok(())
    })();

    stop.stop();
    drop(output);
    let _ = watcher.join();
    result?;

    let status = station.finish()?;
    eprintln!(
        "DRM stopped after {} frames ({:.1} seconds); {} clipped samples.",
        status.frames, status.seconds, status.clipped_samples
    );
    Ok(())
}
