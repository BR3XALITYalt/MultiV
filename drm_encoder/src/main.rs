// SPDX-License-Identifier: GPL-2.0-or-later

use decdrm_station::{SampleFormat, SignalFormat, Station, StationConfig, StopHandle};
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
    let first_arg = args.next().ok_or("missing station configuration path")?;
    let check_only = first_arg == std::ffi::OsStr::new("--check");
    let config_path = if check_only {
        args.next().map(PathBuf::from).ok_or("missing station configuration path after --check")?
    } else {
        PathBuf::from(first_arg)
    };
    let stop_path = if check_only {
        None
    } else {
        Some(args.next().map(PathBuf::from).ok_or("missing stop-file path")?)
    };
    if args.next().is_some() {
        return Err("unexpected command-line argument".into());
    }

    let mut config = StationConfig::load(config_path)?;
    config.output.file = None;
    config.output.device = None;
    config.output.sample_format = SampleFormat::Float32;
    config.output.format = SignalFormat::Real;
    // Mode B / SO0 carriers plus the 150 Hz filter margin stay below TXITY's 6 kHz edge.
    config.output.if_hz = Some(1_500.0);

    if check_only {
        let plan = config.validate()?;
        println!(
            "Valid DRM station: {} service(s), {} stream(s), {}.",
            config.services.len(),
            plan.streams.len(),
            plan.layout
        );
        return Ok(());
    }

    let stop_path = stop_path.ok_or("missing stop-file path")?;
    let stop = StopHandle::default();
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

    let plan = match config.validate() {
        Ok(plan) => plan,
        Err(error) => {
            stop.stop();
            let _ = watcher.join();
            return Err(error.into());
        }
    };
    eprintln!("DRM channel plan:\n{}", plan.describe(&config));
    let mut station = match Station::with_plan_and_stop(config, plan, stop.clone()) {
        Ok(station) => station,
        Err(error) => {
            stop.stop();
            let _ = watcher.join();
            return Err(error.into());
        }
    };
    if station.output_channels() != 1 {
        stop.stop();
        let _ = watcher.join();
        return Err("the DRM bridge requires one real-IF output channel".into());
    }

    let mut output = io::stdout().lock();
    let mut bytes = Vec::with_capacity(19_200 * 4);
    let frame_period = Duration::from_millis(400);
    let mut next_diagnostic = Instant::now() + Duration::from_secs(5);
    let mut timing_frames = 0u64;
    let mut timing_overruns = 0u64;
    let mut timing_total = Duration::ZERO;
    let mut timing_max = Duration::ZERO;
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

            let frame_elapsed = frame_start.elapsed();
            timing_frames += 1;
            timing_total += frame_elapsed;
            timing_max = timing_max.max(frame_elapsed);
            if frame_elapsed >= frame_period {
                timing_overruns += 1;
            }

            for line in station.take_log() {
                eprintln!("DRM source: {line}");
            }

            if Instant::now() >= next_diagnostic {
                let status = station.status();
                eprintln!(
                    "DRM output diagnostics: frames={}, signal={:.1}s, RMS={:.1} dBFS, peak={:.1} dBFS, clipped={}.",
                    status.frames,
                    status.seconds,
                    status.output_rms_dbfs,
                    status.output_peak_dbfs,
                    status.clipped_samples
                );
                log_audio_diagnostics(status, "live");
                if timing_frames > 0 {
                    eprintln!(
                        "DRM frame timing: avg={:.1} ms, max={:.1} ms, over-400ms={}/{}.",
                        timing_total.as_secs_f64() * 1000.0 / timing_frames as f64,
                        timing_max.as_secs_f64() * 1000.0,
                        timing_overruns,
                        timing_frames
                    );
                }
                timing_frames = 0;
                timing_overruns = 0;
                timing_total = Duration::ZERO;
                timing_max = Duration::ZERO;
                next_diagnostic = Instant::now() + Duration::from_secs(5);
            }

            let remaining = frame_period.saturating_sub(frame_elapsed);
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
        "DRM stopped after {} frames ({:.1} seconds); output RMS={:.1} dBFS, peak={:.1} dBFS, {} clipped samples.",
        status.frames,
        status.seconds,
        status.output_rms_dbfs,
        status.output_peak_dbfs,
        status.clipped_samples
    );
    log_audio_diagnostics(&status, "final");
    Ok(())
}

fn log_audio_diagnostics(status: &decdrm_station::StationStatus, phase: &str) {
    for service in &status.services {
        let Some(audio) = &service.audio else {
            continue;
        };
        let counters = &audio.counters;
        let drift = counters
            .input_drift_ppm
            .map(|ppm| format!("{ppm:+.0} ppm"))
            .unwrap_or_else(|| "not available".to_string());
        eprintln!(
            "DRM audio diagnostics ({phase}): service=\"{}\", input={:.1} dBFS RMS/{:.1} dBFS peak, stream={:.2} kbit/s, encoder={:.2} kbit/s, superframes={}, dropped={}, encoder_errors={}, input_drift={}.",
            service.label,
            counters.input_rms_dbfs,
            counters.input_peak_dbfs,
            audio.stream_bitrate / 1000.0,
            audio.encoder_bitrate as f64 / 1000.0,
            counters.super_frames,
            counters.frames_dropped,
            counters.encoder_errors,
            drift
        );
    }
}
