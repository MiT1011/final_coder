"""System-audio (WASAPI loopback) listener using the `soundcard` library.

Captures whatever is playing through the default Windows output device — i.e.
the interviewer's voice in Zoom/Teams/Meet — segments it on a short silence
boundary, and hands each utterance off to a transcription callback.

`soundcard` exposes loopback as a regular microphone via
`get_microphone(id=..., include_loopback=True)`. That works in stock releases,
unlike `sounddevice.WasapiSettings(loopback=...)` which is not in mainline.

All UI feedback goes through caller-supplied callbacks; this module does no
Tk work itself.
"""
import io, wave, queue, threading, logging
import numpy as np
import soundcard as sc
import config as cfg

log = logging.getLogger(__name__)


class AudioCaptureError(RuntimeError):
    pass


class AudioListener:
    def __init__(self, transcribe_fn, on_transcript, on_status,
                 on_segment_start=None, on_segment_end=None):
        self.transcribe_fn = transcribe_fn
        self.on_transcript = on_transcript
        self.on_status = on_status
        self.on_segment_start = on_segment_start
        self.on_segment_end = on_segment_end

        self._queue = queue.Queue(maxsize=cfg.AUDIO_QUEUE_MAX)
        self._capture_thread = None
        self._worker_thread = None
        self._running = False
        self._mic = None

        # Stream params (resolved on start)
        self._sample_rate = 48000
        self._block_size = 4800
        self._silence_blocks_needed = 12
        self._min_speech_blocks = 6
        self._max_segment_blocks = 300

        # Segmentation state — only touched on capture thread
        self._buf = []
        self._speech_blocks = 0
        self._silence_blocks = 0
        self._speaking = False
        self._segment_announced = False

    # ----- public API -----
    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            log.debug("AudioListener.start() called but already running")
            return
        try:
            speaker = sc.default_speaker()
            log.info("Default speaker: %s", speaker.name)
        except Exception as e:
            log.exception("default_speaker() failed")
            raise AudioCaptureError(f"No default speaker available: {e}")

        try:
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            log.info("Loopback mic acquired: %s", mic.name)
        except Exception as e:
            log.exception("get_microphone(loopback) failed")
            raise AudioCaptureError(f"Could not open loopback for '{speaker.name}': {e}")

        block_s = cfg.AUDIO_BLOCK_MS / 1000.0
        self._sample_rate = 48000
        self._block_size  = max(160, int(self._sample_rate * block_s))
        self._silence_blocks_needed = max(4, int(cfg.AUDIO_SILENCE_DURATION_S / block_s))
        self._min_speech_blocks     = max(2, int(cfg.AUDIO_MIN_SPEECH_S      / block_s))
        self._max_segment_blocks    = max(self._min_speech_blocks + 1,
                                          int(cfg.AUDIO_MAX_SEGMENT_S / block_s))
        log.info("Audio config: sr=%d block=%d silence_needed=%d min_speech=%d max_segment=%d threshold=%.4f",
                 self._sample_rate, self._block_size,
                 self._silence_blocks_needed, self._min_speech_blocks,
                 self._max_segment_blocks, cfg.AUDIO_SILENCE_THRESHOLD)

        self._mic = mic
        self._reset_segment()
        self._running = True

        self._worker_thread = threading.Thread(target=self._worker_loop,
                                               name="AudioWorker", daemon=True)
        self._worker_thread.start()
        self._capture_thread = threading.Thread(target=self._capture_loop,
                                                name="AudioCapture", daemon=True)
        self._capture_thread.start()
        log.info("AudioListener started")

    def stop(self):
        if not self._running and self._capture_thread is None and self._worker_thread is None:
            return
        log.info("AudioListener stopping")
        self._running = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._reset_segment()
        self._mic = None

    # ----- internals -----
    def _reset_segment(self):
        self._buf = []
        self._speech_blocks = 0
        self._silence_blocks = 0
        self._speaking = False
        self._segment_announced = False

    def _capture_loop(self):
        log.info("Capture loop starting")
        try:
            with self._mic.recorder(samplerate=self._sample_rate,
                                    channels=1,
                                    blocksize=self._block_size) as rec:
                log.info("Recorder opened")
                while self._running:
                    try:
                        data = rec.record(numframes=self._block_size)
                    except Exception as e:
                        log.exception("rec.record() failed")
                        self._notify_status(f"Capture error: {e}")
                        break
                    if not self._running:
                        break
                    try:
                        self._process_block(data)
                    except Exception:
                        log.exception("_process_block() failed")
        except Exception as e:
            log.exception("Capture loop init failed")
            self._notify_status(f"Capture failed: {e}")
        finally:
            log.info("Capture loop exited")

    def _process_block(self, indata):
        if indata is None or len(indata) == 0:
            return
        if indata.ndim == 1:
            mono = indata
        else:
            mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
        voiced = rms > cfg.AUDIO_SILENCE_THRESHOLD

        if voiced:
            if not self._speaking:
                self._speaking = True
                self._segment_announced = False
                log.debug("Speech started (rms=%.4f)", rms)
            self._silence_blocks = 0
            self._speech_blocks += 1
            self._buf.append(mono.copy())
            if (not self._segment_announced) and self._speech_blocks >= self._min_speech_blocks:
                self._segment_announced = True
                self._notify_segment_start()
            if len(self._buf) >= self._max_segment_blocks:
                log.info("Max segment length reached — emitting")
                self._emit_segment()
        else:
            if self._speaking:
                self._silence_blocks += 1
                self._buf.append(mono.copy())
                if self._silence_blocks >= self._silence_blocks_needed:
                    if self._speech_blocks >= self._min_speech_blocks:
                        self._emit_segment()
                    else:
                        log.debug("Short noise burst dropped (speech_blocks=%d)", self._speech_blocks)
                        self._reset_segment()

    def _emit_segment(self):
        try:
            audio = np.concatenate(self._buf) if self._buf else None
        except Exception:
            log.exception("Buffer concat failed")
            audio = None
        speech_blocks = self._speech_blocks
        self._reset_segment()
        if audio is None or audio.size == 0:
            return
        duration_s = audio.size / float(self._sample_rate)
        log.info("Emitting segment: %.2fs (%d voiced blocks)", duration_s, speech_blocks)
        try:
            wav = self._to_wav(audio, self._sample_rate)
        except Exception as e:
            log.exception("WAV encode failed")
            self._notify_status(f"WAV encode error: {e}")
            return
        try:
            self._queue.put_nowait(wav)
            log.debug("Queued wav: %d bytes (qsize=%d)", len(wav), self._queue.qsize())
            self._notify_segment_end()
        except queue.Full:
            log.warning("Audio queue full — dropping segment")
            self._notify_status("Audio backlog full — dropping segment.")

    @staticmethod
    def _to_wav(audio_f32, sr):
        clipped = np.clip(audio_f32, -1.0, 1.0)
        audio_i16 = (clipped * 32767.0).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio_i16.tobytes())
        return buf.getvalue()

    def _worker_loop(self):
        log.info("Worker loop starting")
        while True:
            try:
                item = self._queue.get(timeout=0.3)
            except queue.Empty:
                if not self._running:
                    break
                continue
            if item is None:
                break
            log.info("Transcribing %d-byte WAV", len(item))
            try:
                text = self.transcribe_fn(item)
            except Exception as e:
                log.exception("Transcription failed")
                self._notify_status(f"Transcription error: {e}")
                continue
            preview = (text or "").strip()
            log.info("Transcript (%d chars): %r", len(preview), preview[:120])
            if preview:
                try:
                    self.on_transcript(preview)
                except Exception:
                    log.exception("on_transcript callback failed")
        log.info("Worker loop exited")

    # ----- safe callback helpers -----
    def _notify_status(self, msg):
        if self.on_status:
            try: self.on_status(msg)
            except Exception: log.exception("on_status callback failed")

    def _notify_segment_start(self):
        if self.on_segment_start:
            try: self.on_segment_start()
            except Exception: log.exception("on_segment_start callback failed")

    def _notify_segment_end(self):
        if self.on_segment_end:
            try: self.on_segment_end()
            except Exception: log.exception("on_segment_end callback failed")


# ============================================================================
# Manual recording mode — continuous capture, no silence segmentation.
# User-controlled start/stop. On stop, the recording is split into <=24 MB
# WAV chunks and handed back to the caller for transcription.
# ============================================================================

_WAV_HEADER_BYTES = 44
_BYTES_PER_SAMPLE = 2  # mono int16


def _int16_to_wav(audio_i16, sample_rate):
    """Wrap an int16 mono numpy array in a WAV container, return bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_BYTES_PER_SAMPLE)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_i16.tobytes())
    return buf.getvalue()


def chunk_wav_for_whisper(audio_i16, sample_rate, max_bytes):
    """Split a mono int16 PCM array into WAV chunks each <= max_bytes.

    Returns a list of WAV-formatted byte blobs. Each blob is independently
    transcribable. Splitting happens at sample boundaries (no fades/crossfades),
    so a word may be cut at the boundary — acceptable trade-off versus dropping
    audio entirely.
    """
    if max_bytes <= _WAV_HEADER_BYTES + sample_rate * _BYTES_PER_SAMPLE:
        raise ValueError(f"max_bytes={max_bytes} is too small (need >= 1s of audio + header)")
    samples_per_chunk = (max_bytes - _WAV_HEADER_BYTES) // _BYTES_PER_SAMPLE
    chunks = []
    total = len(audio_i16)
    for start in range(0, total, samples_per_chunk):
        seg = audio_i16[start:start + samples_per_chunk]
        if seg.size == 0:
            continue
        chunks.append(_int16_to_wav(seg, sample_rate))
    log.info("Chunked %.2fs audio into %d WAV blob(s) (max=%d bytes)",
             total / float(sample_rate), len(chunks), max_bytes)
    return chunks


class ManualRecorder:
    """Continuous loopback recorder. start()/stop() control it; on stop the
    raw int16 buffer is returned. UI feedback comes through `on_progress`
    (called every ~1s with elapsed seconds) and `on_status` (errors).
    """
    def __init__(self, on_progress=None, on_status=None):
        self.on_progress = on_progress
        self.on_status = on_status
        self._thread = None
        self._running = False
        self._mic = None
        self._buf = []
        self._sample_rate = 48000
        self._block_size = max(160, int(self._sample_rate * (cfg.AUDIO_BLOCK_MS / 1000.0)))
        self._max_blocks = int(cfg.MANUAL_RECORD_MAX_S * self._sample_rate / self._block_size)

    @property
    def running(self):
        return self._running

    def start(self):
        if self._running:
            log.debug("ManualRecorder.start() called while already running")
            return
        try:
            speaker = sc.default_speaker()
            log.info("ManualRecorder: default speaker=%s", speaker.name)
        except Exception as e:
            log.exception("default_speaker() failed (manual)")
            raise AudioCaptureError(f"No default speaker: {e}")
        try:
            self._mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            log.info("ManualRecorder: loopback mic=%s", self._mic.name)
        except Exception as e:
            log.exception("loopback acquire failed (manual)")
            raise AudioCaptureError(f"Could not open loopback for '{speaker.name}': {e}")

        self._buf = []
        self._running = True
        self._thread = threading.Thread(target=self._record_loop,
                                        name="ManualRec", daemon=True)
        self._thread.start()
        log.info("ManualRecorder started (sr=%d block=%d max=%ds)",
                 self._sample_rate, self._block_size, cfg.MANUAL_RECORD_MAX_S)

    def stop(self):
        """Stop recording, join the capture thread, return (int16_audio, sample_rate).
        Returns (None, sr) if nothing was captured.
        """
        if not self._running and not self._buf:
            return None, self._sample_rate
        log.info("ManualRecorder stopping; buf_blocks=%d", len(self._buf))
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if not self._buf:
            return None, self._sample_rate
        try:
            audio_f32 = np.concatenate(self._buf)
        except Exception:
            log.exception("ManualRecorder concat failed")
            return None, self._sample_rate
        self._buf = []
        audio_i16 = (np.clip(audio_f32, -1.0, 1.0) * 32767.0).astype(np.int16)
        log.info("ManualRecorder produced %.2fs audio (%d samples)",
                 audio_i16.size / float(self._sample_rate), audio_i16.size)
        return audio_i16, self._sample_rate

    def _record_loop(self):
        log.info("ManualRec capture loop starting")
        try:
            with self._mic.recorder(samplerate=self._sample_rate,
                                    channels=1,
                                    blocksize=self._block_size) as rec:
                blocks = 0
                last_progress = 0
                while self._running:
                    try:
                        data = rec.record(numframes=self._block_size)
                    except Exception as e:
                        log.exception("ManualRec rec.record failed")
                        if self.on_status:
                            try: self.on_status(f"Capture error: {e}")
                            except Exception: pass
                        break
                    if not self._running:
                        break
                    if data.ndim > 1 and data.shape[1] > 1:
                        mono = data.mean(axis=1)
                    elif data.ndim > 1:
                        mono = data[:, 0]
                    else:
                        mono = data
                    self._buf.append(mono.copy())
                    blocks += 1
                    if blocks - last_progress >= 10:  # ~1 second
                        last_progress = blocks
                        elapsed = blocks * (self._block_size / float(self._sample_rate))
                        if self.on_progress:
                            try: self.on_progress(elapsed)
                            except Exception: log.exception("on_progress callback failed")
                    if blocks >= self._max_blocks:
                        log.warning("ManualRec hit max duration (%ds), auto-stopping",
                                    cfg.MANUAL_RECORD_MAX_S)
                        self._running = False
                        if self.on_status:
                            try: self.on_status(f"Max recording length ({cfg.MANUAL_RECORD_MAX_S}s) reached.")
                            except Exception: pass
                        break
        except Exception as e:
            log.exception("ManualRec init failed")
            if self.on_status:
                try: self.on_status(f"Recorder failed: {e}")
                except Exception: pass
        finally:
            log.info("ManualRec capture loop exited (blocks=%d)", len(self._buf))
