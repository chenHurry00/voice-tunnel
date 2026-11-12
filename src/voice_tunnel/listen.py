"""Push-to-talk listener: evdev key -> microphone -> faster-whisper -> voice-tunnel."""

from __future__ import annotations

import fcntl
import logging
import os
import queue
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import VoiceConfig

LOG = logging.getLogger("voice_tunnel.listen")

QWEN_BACKENDS = {"qwen3", "qwen", "qwen3-asr"}
# partial 只解码最近一段音频，避免长句时重解码延迟线性增长
QWEN_PARTIAL_WINDOW_SEC = 8.0


# --------------------------------------------------------------------------- qwen service


def _to_simplified(text: str) -> str:
    try:
        from zhconv import convert

        return convert(text, "zh-cn")
    except Exception:  # noqa: BLE001
        return text


def _qwen_health(url: str, timeout: float = 1.0) -> bool:
    base = url.rsplit("/", 1)[0]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_qwen_service(config: VoiceConfig) -> None:
    """Qwen3-ASR 服务未启动时自动拉起（模型常驻，避免每次 PTT 重新加载）。"""
    if _qwen_health(config.qwen_url):
        LOG.info("Qwen3-ASR service ready: %s", config.qwen_url)
        return

    data_dir = Path(os.path.expanduser(config.data_dir))
    root = Path(__file__).resolve().parents[2]
    backend = getattr(config, "qwen_backend", "transformers").lower()
    venv_name = "qwen-vllm" if backend == "vllm" else "qwen"
    python = data_dir / "venvs" / venv_name / "bin" / "python"
    if not python.exists():
        python = root / ".venv-qwen" / "bin" / "python"
    script = root / "scripts" / "qwen_asr_service.py"
    model = Path(os.path.expanduser(config.model))
    if not (model / "model.safetensors").exists():
        model = data_dir / "models" / "Qwen3-ASR-0.6B"
    if not python.exists() or not script.exists():
        raise RuntimeError(f"Qwen3-ASR service unavailable: missing {python} or {script}")
    if not (model / "model.safetensors").exists():
        raise RuntimeError(f"Qwen3-ASR weights not found under {model}")

    parsed = urllib.parse.urlparse(config.qwen_url)
    port = parsed.port or 8770
    device = "cuda:0" if config.device_type.lower().startswith("cuda") else "cpu"
    log_path = data_dir / "logs" / "qwen-service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("starting Qwen3-ASR service (%s): %s", backend, python)
    with open(log_path, "ab") as log_file:
        subprocess.Popen(
            [
                str(python),
                str(script),
                "--model",
                str(model),
                "--host",
                parsed.hostname or "127.0.0.1",
                "--port",
                str(port),
                "--device",
                device,
                "--backend",
                backend,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            cwd=str(root),
            start_new_session=True,
        )
    deadline = time.time() + 180
    while time.time() < deadline:
        if _qwen_health(config.qwen_url):
            LOG.info("Qwen3-ASR service ready: %s", config.qwen_url)
            return
        time.sleep(0.5)
    raise RuntimeError(f"Qwen3-ASR service did not become healthy; see {log_path}")


# --------------------------------------------------------------------------- audio


class AudioRecorder:
    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self.frames: list[Any] = []
        self.levels: list[float] = []
        self.stream: Any = None
        self.capture_rate = config.sample_rate
        self.lock = threading.Lock()

    def _device_index(self) -> int | None:
        import sounddevice as sd

        wanted = self.config.device.strip()
        if wanted.isdigit():
            return int(wanted)
        for index, device in enumerate(sd.query_devices()):
            if device.get("max_input_channels", 0) <= 0:
                continue
            if wanted.lower() in str(device.get("name", "")).lower():
                return index
        return None

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        import numpy as np

        if status:
            LOG.warning("audio status: %s", status)
        data = indata.astype(np.float32).flatten()
        rms = float(np.sqrt(np.mean(data * data))) / 32768.0 if len(data) else 0.0
        with self.lock:
            self.frames.append(indata.copy())
            self.levels.append(rms)
            if len(self.levels) > 20:
                del self.levels[:-20]

    def recent_level(self) -> float:
        with self.lock:
            return max(self.levels[-8:]) if self.levels else 0.0

    def start(self) -> None:
        import sounddevice as sd

        device = self._device_index()
        errors: list[str] = []
        for rate in (self.config.sample_rate, 48000, 44100):
            try:
                sd.check_input_settings(
                    device=device, channels=self.config.channels, dtype="int16", samplerate=rate
                )
                self.capture_rate = rate
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rate}: {exc}")
        else:
            raise RuntimeError("no supported input sample rate: " + "; ".join(errors))
        with self.lock:
            self.frames = []
            self.levels = []
        self.stream = sd.InputStream(
            device=device,
            channels=self.config.channels,
            dtype="int16",
            samplerate=self.capture_rate,
            callback=self._callback,
        )
        self.stream.start()
        LOG.info("recording at %s Hz (device=%s)", self.capture_rate, device if device is not None else "default")

    def _resample(self, audio: Any) -> Any:
        import numpy as np

        if self.capture_rate == self.config.sample_rate or len(audio) == 0:
            return audio
        target_len = max(1, int(len(audio) * self.config.sample_rate / self.capture_rate))
        return np.interp(
            np.linspace(0, len(audio), target_len, endpoint=False),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)

    def snapshot(self) -> Any:
        import numpy as np

        with self.lock:
            if not self.frames:
                return np.zeros(0, dtype=np.float32)
            data = np.concatenate(self.frames).flatten()
        audio = data.astype(np.float32) / 32768.0
        audio = trim_silence(audio, self.capture_rate)
        audio = self._resample(audio)
        return normalize_audio(audio)

    def raw_snapshot(self) -> Any:
        """流式专用：未裁剪、未归一化的连续原始音频（保证 _stream_sent 索引稳定）。"""
        import numpy as np

        with self.lock:
            if not self.frames:
                return np.zeros(0, dtype=np.float32)
            data = np.concatenate(self.frames).flatten()
        audio = data.astype(np.float32) / 32768.0
        return self._resample(audio)

    def stop(self) -> Any:
        import numpy as np

        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            frames = self.frames
            self.frames = []
        if not frames:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(frames).flatten().astype(np.float32) / 32768.0
        # 流式路径需要与 raw_snapshot 一致的原始音频；裁剪/归一化由使用方按需处理
        return self._resample(audio)


# --------------------------------------------------------------------------- ASR


def trim_silence(audio: Any, rate: int, threshold: float = 0.012) -> Any:
    import numpy as np

    if len(audio) == 0:
        return audio
    index = np.where(np.abs(audio) > threshold)[0]
    if len(index) == 0:
        return audio[:0]
    start = max(0, int(index[0] - 0.10 * rate))
    end = min(len(audio), int(index[-1] + 0.20 * rate))
    return audio[start:end]


def normalize_audio(audio: Any) -> Any:
    import numpy as np

    if len(audio) == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak > 0.001:
        audio = audio * min(8.0, 0.85 / peak)
    return audio.astype(np.float32)


class ASREngine:
    def __init__(self, config: VoiceConfig) -> None:
        self.config = config
        self.model: Any = None
        self.partial_model: Any = None
        self.model_lock = threading.Lock()
        self.partial_lock = threading.Lock()
        self.stream_id = ""

    def load(self) -> None:
        if self.config.backend.lower() in QWEN_BACKENDS:
            ensure_qwen_service(self.config)
            return
        from faster_whisper import WhisperModel

        kwargs = {
            "device": self.config.device_type,
            "compute_type": self.config.compute_type,
        }
        if self.config.download_root:
            kwargs["download_root"] = os.path.expanduser(self.config.download_root)
        model_path = os.path.expanduser(self.config.model)
        LOG.info("loading ASR model %s (%s/%s)", model_path, self.config.device_type, self.config.compute_type)
        self.model = WhisperModel(model_path, **kwargs)
        if self.config.partial:
            partial_path = os.path.expanduser(self.config.partial_model)
            LOG.info("loading partial ASR model %s", partial_path)
            self.partial_model = WhisperModel(partial_path, **kwargs)
        LOG.info("ASR ready")

    def streaming(self) -> bool:
        return (
            self.config.backend.lower() in QWEN_BACKENDS
            and getattr(self.config, "qwen_backend", "transformers").lower() == "vllm"
        )

    def _service_post(self, path: str, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        import json

        base = self.config.qwen_url.rsplit("/", 1)[0]
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "qwen service error"))
        return result

    def start_stream(self) -> bool:
        self.stream_id = ""
        try:
            result = self._service_post(
                "/stream/start",
                {"language": None if self.config.language.lower() in {"", "auto"} else self.config.language},
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("stream start failed, falling back: %s", exc)
            return False
        self.stream_id = str(result.get("session_id", ""))
        return bool(self.stream_id)

    def push_stream(self, audio: Any, final: bool = False) -> str:
        import base64

        import numpy as np

        if not self.stream_id:
            return ""
        payload = {
            "session_id": self.stream_id,
            "audio": base64.b64encode(np.asarray(audio, dtype=np.float32).tobytes()).decode("ascii"),
            "final": final,
        }
        try:
            result = self._service_post("/stream/chunk", payload, timeout=120)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("stream chunk failed: %s", exc)
            if final:
                self.stream_id = ""
            return ""
        if final:
            self.stream_id = ""
        text = str(result.get("text", "")).strip()
        if text and self.config.simplify:
            text = _to_simplified(text)
        return text

    def refresh_stream(self) -> str:
        """停顿微调：整段不带前缀重解码，返回修正后的全文。"""
        if not self.stream_id:
            return ""
        try:
            result = self._service_post("/stream/refresh", {"session_id": self.stream_id}, timeout=120)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("stream refresh failed: %s", exc)
            return ""
        text = str(result.get("text", "")).strip()
        if text and self.config.simplify:
            text = _to_simplified(text)
        return text

    def close_stream(self) -> None:
        if not self.stream_id:
            return
        try:
            self._service_post("/stream/cancel", {"session_id": self.stream_id}, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.stream_id = ""

    def _transcribe_qwen(self, audio: Any) -> str:
        import base64

        import numpy as np

        payload = {
            "audio": base64.b64encode(np.asarray(audio, dtype=np.float32).tobytes()).decode("ascii"),
            "sample_rate": self.config.sample_rate,
            "language": None if self.config.language.lower() in {"", "auto"} else self.config.language,
        }
        result = self._service_post("/transcribe", payload, timeout=120)
        return str(result.get("text", "")).strip()

    def transcribe(self, audio: Any, partial: bool = False) -> str:
        if self.config.backend.lower() in QWEN_BACKENDS:
            text = self._transcribe_qwen(audio)
            if text and self.config.simplify:
                text = _to_simplified(text)
            return text
        model = self.partial_model if partial and self.partial_model is not None else self.model
        if model is None:
            return ""
        language = None if self.config.language.lower() in {"", "auto"} else self.config.language
        lock = self.partial_lock if partial else self.model_lock
        with lock:
            segments, _info = model.transcribe(
                audio,
                language=language,
                beam_size=1 if partial else self.config.beam_size,
                vad_filter=False,
                condition_on_previous_text=False,
                without_timestamps=True,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )
            text = "".join(segment.text for segment in segments).strip()
        if text and self.config.simplify:
            text = _to_simplified(text)
        return text


# --------------------------------------------------------------------------- PTT


def _import_evdev() -> Any:
    try:
        import evdev  # type: ignore
    except ImportError as exc:
        raise RuntimeError("python-evdev is not installed; run: pip install -e '.[voice]'") from exc
    return evdev


def open_ptt_device(config: VoiceConfig) -> Any:
    evdev = _import_evdev()
    path = config.ptt_device
    try:
        return evdev.InputDevice(path), evdev
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise RuntimeError(
            f"cannot open PTT device {path}: {exc}\n"
            "Check the path or add a udev uaccess rule for the keyboard."
        ) from exc


class PttListener(threading.Thread):
    def __init__(self, config: VoiceConfig, on_press: Any, on_release: Any) -> None:
        super().__init__(daemon=True, name="ptt-listener")
        self.config = config
        self.on_press = on_press
        self.on_release = on_release
        self.device, self.evdev = open_ptt_device(config)
        self.keycode = getattr(self.evdev.ecodes, config.ptt_key, None)
        if self.keycode is None:
            raise RuntimeError(f"unknown evdev key name: {config.ptt_key}")
        self.stop_event = threading.Event()

    def run(self) -> None:
        LOG.info("PTT ready: %s (%s)", self.config.ptt_key, self.device.path)
        try:
            for event in self.device.read_loop():
                if self.stop_event.is_set():
                    return
                if event.type != self.evdev.ecodes.EV_KEY or event.code != self.keycode:
                    continue
                if event.value == 1:
                    self.on_press()
                elif event.value == 0:
                    self.on_release()
        except (OSError, ValueError):
            if not self.stop_event.is_set():
                LOG.exception("PTT device read failed")

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.device.close()
        except OSError:
            pass


def keytest(config: VoiceConfig, seconds: float = 20.0, once: bool = False) -> None:
    device, evdev = open_ptt_device(config)
    print(f"device: {device.path}  name: {device.name}", flush=True)
    print(f"press keys for {seconds:.0f}s; Ctrl+C to stop", flush=True)
    deadline = time.time() + seconds
    try:
        for event in device.read_loop():
            if event.type == evdev.ecodes.EV_KEY and event.value == 1:
                name = evdev.ecodes.KEY.get(event.code, str(event.code))
                print(f"{name} (code={event.code})", flush=True)
                if once:
                    return
            if time.time() > deadline:
                break
    finally:
        device.close()


# --------------------------------------------------------------------------- listener


class VoiceListener:
    def __init__(self, config: VoiceConfig, config_path: str) -> None:
        self.config = config
        self.config_path = config_path
        self.recorder = AudioRecorder(config)
        self.asr = ASREngine(config)
        self.recording = False
        self.record_started = 0.0
        self.partial_thread: threading.Thread | None = None
        self.partial_stop = threading.Event()
        self.final_queue: queue.Queue[Any] = queue.Queue()
        self.worker = threading.Thread(target=self._worker, daemon=True, name="asr-worker")
        self.max_timer: threading.Timer | None = None
        self.ptt: PttListener | None = None
        self.prev_partial = ""
        self.last_partial_shown = ""
        self._lock_handle: Any = None
        self._stream_active = False
        self._stream_sent = 0
        self._silence_ticks = 0
        self._stable_ticks = 0
        self._peak_level = 0.0
        self._stream_lock = threading.Lock()

    def _send_control(self, message: dict[str, Any]) -> dict[str, Any]:
        from .cli import control_request

        return control_request(self.config_path, message, timeout=20)

    def _notify_partial(self, text: str) -> None:
        try:
            self._send_control(
                {"command": "preview", "target": self.config.target, "text": text, "stable_len": -1}
            )
        except Exception as exc:  # noqa: BLE001
            LOG.debug("partial display failed: %s", exc)

    def on_press(self) -> None:
        if self.recording:
            return
        self.recording = True
        self.record_started = time.time()
        self.prev_partial = ""
        self.last_partial_shown = ""
        self._stream_active = False
        self._peak_level = 0.0
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            self.recording = False
            LOG.error("recording start failed: %s", exc)
            return
        self.partial_stop = threading.Event()
        self._stream_sent = 0
        self._stream_active = self.asr.streaming() and self.asr.start_stream()
        if self.config.partial:
            self.partial_thread = threading.Thread(target=self._partial_loop, daemon=True, name="partial-asr")
            self.partial_thread.start()
        if self.config.max_seconds > 0:
            self.max_timer = threading.Timer(self.config.max_seconds, self._auto_stop)
            self.max_timer.daemon = True
            self.max_timer.start()

    def _auto_stop(self) -> None:
        if self.recording:
            LOG.warning("PTT max duration reached, stopping")
            self.on_release()

    def on_release(self) -> None:
        if not self.recording:
            return
        self.recording = False
        if self.max_timer is not None:
            self.max_timer.cancel()
            self.max_timer = None
        self.partial_stop.set()
        if self.partial_thread is not None:
            self.partial_thread.join(timeout=5 if self._stream_active else 0.1)
            self.partial_thread = None
        try:
            audio = self.recorder.stop()
        except Exception as exc:  # noqa: BLE001
            LOG.error("recording stop failed: %s", exc)
            self.asr.close_stream()
            self._stream_active = False
            return
        duration = len(audio) / self.config.sample_rate if len(audio) else 0.0
        if duration < self.config.min_seconds:
            LOG.info("ignore short utterance: %.2fs", duration)
            self.asr.close_stream()
            self._stream_active = False
            return
        LOG.info("utterance captured: %.2fs", duration)
        if self._stream_active:
            text = ""
            with self._stream_lock:
                delta = audio[self._stream_sent :]
                self._stream_sent = len(audio)
                if len(delta):
                    self.asr.push_stream(delta)
                # 松手收尾用无前缀整段重解码（refresh），避免 finish 的固定前缀造成复读
                text = self.asr.refresh_stream()
            self.asr.close_stream()
            self._stream_active = False
            if text:
                self.final_queue.put(("text", text))
                return
            LOG.warning("stream refresh empty, falling back to full decode")
        self.final_queue.put(("audio", audio))

    @staticmethod
    def _common_prefix(left: str, right: str) -> str:
        limit = min(len(left), len(right))
        index = 0
        while index < limit and left[index] == right[index]:
            index += 1
        return left[:index]

    @staticmethod
    def _clean_partial(text: str) -> str:
        import re

        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"([。！？!?，,、.])\1+", r"\1", text)
        repeated = re.search(r"(.{1,8}?)\1{2,}", text)
        if repeated:
            text = text[: repeated.start()].strip()
        return text

    def _partial_loop(self) -> None:
        window_sec = QWEN_PARTIAL_WINDOW_SEC if self.config.backend.lower() in QWEN_BACKENDS else 0.0
        window_start = 0
        while not self.partial_stop.wait(self.config.partial_interval):
            if not self.recording:
                return
            if self._stream_active:
                with self._stream_lock:
                    audio = self.recorder.raw_snapshot()
                    delta = audio[self._stream_sent :]
                    self._stream_sent = len(audio)
                    # 自适应静音阈值：相对本次说话的峰值，避免底噪高于固定阈值时永远判不出"停顿"
                    level = self.recorder.recent_level()
                    self._peak_level = max(self._peak_level, level)
                    silent = level < max(0.004, self._peak_level * 0.06)
                    self._silence_ticks = self._silence_ticks + 1 if silent else 0
                    text = ""
                    if len(delta):
                        text = self.asr.push_stream(delta)
                    refresh_now = False
                    if self._silence_ticks == 3:  # 静音约 1.5s：认为这一句结束
                        self._silence_ticks += 1  # 每次停顿只触发一次，再说话归零
                        refresh_now = True
                    if text and text == self.last_partial_shown:
                        self._stable_ticks += 1
                    else:
                        self._stable_ticks = 0
                    if self._stable_ticks == 4:  # 文本 3s 没变化：也触发一次修正兜底
                        self._stable_ticks = 0
                        refresh_now = True
                    if refresh_now and self.asr.stream_id:
                        refreshed = self.asr.refresh_stream()
                        if refreshed:
                            text = refreshed
                if text and text != self.last_partial_shown:
                    self.last_partial_shown = text
                    self._notify_partial(text)
                continue
            if self.recorder.recent_level() < 0.004:
                continue
            audio = self.recorder.snapshot()
            if len(audio) < self.config.sample_rate * 1.0:
                continue
            if window_sec > 0:
                keep = int(window_sec * self.config.sample_rate)
                if len(audio) > keep:
                    start = len(audio) - keep
                    if start != window_start:
                        window_start = start
                        self.prev_partial = ""
                    audio = audio[start:]
            try:
                text = self._clean_partial(self.asr.transcribe(audio, partial=True))
                if not text:
                    continue
                if not self.prev_partial:
                    self.prev_partial = text
                    continue
                prefix = self._common_prefix(self.prev_partial, text)
                self.prev_partial = text
                if len(prefix) < 2 or prefix == self.last_partial_shown:
                    continue
                self.last_partial_shown = prefix
                LOG.debug("partial stable: %s", prefix)
                self._notify_partial(prefix)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("partial ASR failed: %s", exc)

    def _worker(self) -> None:
        while True:
            item = self.final_queue.get()
            if item is None:
                return
            kind, payload = item
            if kind == "text":
                text = payload
            else:
                try:
                    # 兜底整段识别：补上 trim/normalize（stop 现在返回原始音频）
                    processed = normalize_audio(trim_silence(payload, self.config.sample_rate))
                    text = self.asr.transcribe(processed, partial=False)
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("ASR failed: %s", exc)
                    continue
            if not text:
                LOG.info("ASR returned empty text")
                continue
            LOG.info("ASR: %s", text)
            try:
                result = self._send_control({"command": "send", "target": self.config.target, "text": text})
                if not result.get("ok"):
                    LOG.error("insert failed: %s", result.get("error"))
                else:
                    LOG.info("inserted into %s", result.get("data", {}).get("target"))
            except Exception as exc:  # noqa: BLE001
                LOG.error("dispatch failed: %s", exc)

    def run(self) -> None:
        self._lock_handle = open("/tmp/voice-tunnel-listen.lock", "w")
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.error("another voice-tunnel listener is already running")
            self._lock_handle.close()
            self._lock_handle = None
            return
        self._lock_handle.write(str(os.getpid()))
        self._lock_handle.flush()
        try:
            self.asr.load()
        except Exception as exc:  # noqa: BLE001
            LOG.error("ASR init failed: %s", exc)
            self.stop()
            return
        self.worker.start()
        self.ptt = PttListener(self.config, self.on_press, self.on_release)
        self.ptt.start()
        LOG.info(
            "voice listener ready: hold %s to talk (target=%s, model=%s)",
            self.config.ptt_key,
            self.config.target,
            self.config.model,
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self.ptt is not None:
            self.ptt.stop()
        self.partial_stop.set()
        self.final_queue.put(None)
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
                self._lock_handle.close()
            except OSError:
                pass
            self._lock_handle = None
