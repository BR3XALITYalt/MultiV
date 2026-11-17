"""Streaming decoders for TXITPY playlist audio."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator


FFMPEG_AUDIO_EXTENSIONS = frozenset({".m4a", ".opus"})


def iter_playlist_audio_blocks(
    file_path: str | Path,
    block_frames: int = 8_192,
) -> Iterator[tuple[int, object]]:
    """Yield bounded float32 audio blocks as (source sample rate, samples)."""
    if block_frames < 1:
        raise ValueError("block_frames must be greater than zero.")

    path = Path(file_path)
    if path.suffix.casefold() in FFMPEG_AUDIO_EXTENSIONS:
        yield from _iter_pyav_blocks(path, block_frames)
        return

    import soundfile

    with soundfile.SoundFile(str(path)) as source:
        sample_rate = int(source.samplerate)
        if sample_rate <= 0 or source.channels <= 0:
            raise RuntimeError(f"Audio file has an invalid format: {path}")

        while True:
            block = source.read(
                block_frames,
                dtype="float32",
                always_2d=True,
            )
            if block.size == 0:
                break
            yield sample_rate, block


def _iter_pyav_blocks(
    path: Path,
    block_frames: int,
) -> Iterator[tuple[int, object]]:
    """Decode M4A and Ogg Opus through the FFmpeg libraries bundled with PyAV."""
    try:
        import av
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "M4A and Opus playlists require the PyAV audio decoder. "
            "Install the TXITPY requirements and restart the application."
        ) from exc

    def converted_blocks(resampler, frame, sample_rate: int):
        for decoded in resampler.resample(frame):
            samples = np.asarray(decoded.to_ndarray(), dtype=np.float32)
            if samples.ndim != 2 or samples.shape[0] != 1:
                raise RuntimeError(
                    f"Could not convert audio from {path} to mono Float32 samples."
                )
            mono = samples[0]
            for offset in range(0, mono.size, block_frames):
                yield sample_rate, mono[offset:offset + block_frames].reshape(-1, 1)

    with av.open(str(path), mode="r") as container:
        audio_stream = next(
            (stream for stream in container.streams if stream.type == "audio"),
            None,
        )
        if audio_stream is None:
            raise RuntimeError(f"No audio stream was found in {path}.")

        source_rate = 0
        source_layout = ""
        resampler = None
        decoded_any = False

        for frame in container.decode(audio_stream):
            decoded_any = True
            frame_rate = int(
                frame.sample_rate
                or audio_stream.codec_context.sample_rate
                or 0
            )
            frame_layout = frame.layout.name if frame.layout is not None else ""
            if frame_rate <= 0 or not frame_layout:
                raise RuntimeError(f"Audio file has an invalid format: {path}")

            if (
                resampler is None
                or frame_rate != source_rate
                or frame_layout != source_layout
            ):
                if resampler is not None:
                    yield from converted_blocks(resampler, None, source_rate)

                resampler = av.AudioResampler(
                    format="fltp",
                    layout="mono",
                    rate=frame_rate,
                )
                source_rate = frame_rate
                source_layout = frame_layout

            yield from converted_blocks(resampler, frame, source_rate)

        if resampler is not None:
            yield from converted_blocks(resampler, None, source_rate)

        if not decoded_any:
            raise RuntimeError(f"No decodable audio frames were found in {path}.")
