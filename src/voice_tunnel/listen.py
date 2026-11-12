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

from .config import VoiceConfig, has_qwen_weights
from .itn import normalize_numbers

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
    if not has_qwen_weights(model):
        fallback = data_dir / "models" / "Qwen3-ASR-0.6B"
        LOG.warning("Qwen3-ASR weights not found under %s, falling back to %s", model, fallback)
        model = fallback
    if not python.exists() or not script.exists():
        raise RuntimeError(f"Qwen3-ASR service unavailable: missing {python} or {script}")
    if not has_qwen_weights(model):
        raise RuntimeError(f"Qwen3-ASR weights not found under {model}")

    parsed = urllib.parse.urlparse(config.qwen_url)
    port = parsed.port or 8770
    device = "cuda:0" if config.device_type.lower().startswith("cuda") else "cpu"
    log_path = data_dir / "logs" / "qwen-service.log"
    extra = ["--gpu-memory-utilization", str(config.qwen_gpu_memory_utilization)] if backend == "vllm" else []
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
                *extra,
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
        self.context = ""  # 领域上下文，由 VoiceListener 在每次按键时刷新

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

    def _post(self, text: str) -> str:
        if text and self.config.simplify:
            text = _to_simplified(text)
        return normalize_numbers(text)

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
                {
                    "language": None if self.config.language.lower() in {"", "auto"} else self.config.language,
                    "context": self.context,
                },
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("stream start failed, falling back: %s", exc)
            return False
        self.stream_id = str(result.get("session_id", ""))
        return bool(self.stream_id)

    def push_stream(self, audio: Any, decode: bool = True) -> str:
        """追加音频；decode=False 时只追加不解码（静音段 / 松手收尾前）。"""
        import base64

        import numpy as np

        if not self.stream_id:
            return ""
        payload = {
            "session_id": self.stream_id,
            "audio": base64.b64encode(np.asarray(audio, dtype=np.float32).tobytes()).decode("ascii"),
            "decode": decode,
        }
        try:
            result = self._service_post("/stream/chunk", payload, timeout=30)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("stream chunk failed: %s", exc)
            return ""
        text = str(result.get("text", "")).strip()
        text = self._post(text)
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
        text = self._post(text)
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
            "context": self.context,
        }
        result = self._service_post("/transcribe", payload, timeout=120)
        return str(result.get("text", "")).strip()

    def transcribe(self, audio: Any, partial: bool = False) -> str:
        if self.config.backend.lower() in QWEN_BACKENDS:
            text = self._transcribe_qwen(audio)
            text = self._post(text)
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
        text = self._post(text)
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
    STREAM_STEP_SEC = 0.12   # 流式推送节拍；解码本身约 0.1s，实际刷新约 0.2s 一次
    SPEECH_HOLD_SEC = 0.8    # 最近这么久内有语音才解码，静音段只追加音频
    PAUSE_REFRESH_SEC = 0.7  # 停顿这么久做一次整段微调（修正前文）

    def __init__(self, config: VoiceConfig, config_path: str) -> None:
        from .control import ControlClient

        self.config = config
        self.config_path = config_path
        self.recorder = AudioRecorder(config)
        self.asr = ASREngine(config)
        # 浮窗事件与文本插入分两条长连接：插入要等 Agent ACK，不能阻塞下一句的浮窗
        self.hud_client = ControlClient(config_path, timeout=3)
        self.send_client = ControlClient(config_path, timeout=20)
        self.recording = False
        self.target: str | None = None
        self.session_thread: threading.Thread | None = None
        self.session_stop = threading.Event()
        self.final_queue: queue.Queue[Any] = queue.Queue()
        self.worker = threading.Thread(target=self._worker, daemon=True, name="asr-worker")
        self.max_timer: threading.Timer | None = None
        self.ptt: PttListener | None = None
        self.shown = ""
        self._lock_handle: Any = None
        self._stream_active = False
        self._stream_sent = 0
        self._speech_since_refresh = False
        self._stream_lock = threading.Lock()

    # ------------------------------------------------------------------ hud / target

    def _resolve_target(self) -> str | None:
        if self.config.target != "active":
            return self.config.target
        try:
            result = self.hud_client.request({"command": "resolve"})
            return (result.get("data") or {}).get("target")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("resolve target failed: %s", exc)
            return None

    def _asr_context(self) -> str:
        """通用描述 + 服务端当前在线的设备名（自动获取，不用手填词表）。"""
        hosts: list[str] = []
        try:
            data = self.hud_client.request({"command": "targets"}).get("data") or []
            hosts = [str(item["host"]) for item in data]
        except Exception as exc:  # noqa: BLE001
            LOG.debug("fetch hosts for ASR context failed: %s", exc)
        return self.config.context + ("设备名：" + "、".join(hosts) + "。" if hosts else "")

    def _hud(self, event: str, target: str | None = None, **fields: Any) -> None:
        target = target or self.target
        if not target:
            return
        try:
            result = self.hud_client.request({"command": "hud", "event": event, "target": target, **fields})
            if not result.get("ok"):
                LOG.debug("hud %s failed: %s", event, result.get("error"))
        except Exception as exc:  # noqa: BLE001
            LOG.debug("hud %s failed: %s", event, exc)

    def _show_partial(self, text: str, settled: bool = False) -> None:
        if not text or (text == self.shown and not settled):
            return
        # 与上一次相同的前缀视为已稳定（深色），新出现/被改写的尾部用浅色
        stable = len(text) if settled else len(self._common_prefix(self.shown, text))
        self.shown = text
        self._hud("partial", text=text, stable=stable, level=self._ui_level())

    def _ui_level(self) -> float:
        rms = self.recorder.recent_level()
        return round(min(1.0, (max(0.0, rms - 0.003) / 0.08) ** 0.5), 2)

    # ------------------------------------------------------------------ PTT

    def on_press(self) -> None:
        if self.recording:
            return
        self.recording = True
        self.shown = ""
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            self.recording = False
            LOG.error("recording start failed: %s", exc)
            return
        self.session_stop = threading.Event()
        self.session_thread = threading.Thread(target=self._session_loop, daemon=True, name="ptt-session")
        self.session_thread.start()
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
        self.session_stop.set()
        if self.session_thread is not None:
            self.session_thread.join(timeout=5)
            self.session_thread = None
        try:
            audio = self.recorder.stop()
        except Exception as exc:  # noqa: BLE001
            LOG.error("recording stop failed: %s", exc)
            self._end_stream()
            self._hud("hide")
            return
        duration = len(audio) / self.config.sample_rate if len(audio) else 0.0
        if duration < self.config.min_seconds:
            LOG.info("ignore short utterance: %.2fs", duration)
            self._end_stream()
            self._hud("hide")
            return
        LOG.info("utterance captured: %.2fs", duration)
        self._hud("finish")
        target = self.target
        if self._stream_active:
            with self._stream_lock:
                if self.shown and not self._speech_since_refresh:
                    # 停顿校正后没再说话：已显示的就是最终结果。再解码只剩静音，模型会编出"嗯。"
                    text = self.shown
                else:
                    delta = audio[self._stream_sent :]
                    self._stream_sent = len(audio)
                    if len(delta):
                        self.asr.push_stream(delta, decode=False)
                    # 松手收尾用无前缀整段重解码（refresh），避免固定前缀造成复读
                    text = self.asr.refresh_stream()
            self._end_stream()
            if text:
                self.final_queue.put(("text", text, target))
                return
            LOG.warning("stream refresh empty, falling back to full decode")
        self.final_queue.put(("audio", audio, target))

    def _end_stream(self) -> None:
        self.asr.close_stream()
        self._stream_active = False

    # ------------------------------------------------------------------ session

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

    def _session_loop(self) -> None:
        # 按下即锁定目标：整句的浮窗和最终插入都发往同一台机器，不随中途切屏漂移
        self.target = self._resolve_target()
        if not self.target:
            LOG.warning("no active target (pointer on a screen without agent?)")
        self._hud("listen", level=0.0)
        self.asr.context = self._asr_context()
        self._stream_sent = 0
        self._stream_active = self.config.partial and self.asr.streaming() and self.asr.start_stream()
        if self._stream_active:
            self._stream_loop()
        else:
            self._window_loop()

    def _stream_loop(self) -> None:
        last_speech = time.time()
        self._speech_since_refresh = False
        peak = 0.0
        while not self.session_stop.wait(self.STREAM_STEP_SEC):
            now = time.time()
            level = self.recorder.recent_level()
            # 自适应静音阈值：相对本次说话的峰值，避免底噪高于固定阈值时永远判不出停顿
            peak = max(peak, level)
            if level >= max(0.004, peak * 0.06):
                last_speech = now
                self._speech_since_refresh = True
            with self._stream_lock:
                audio = self.recorder.raw_snapshot()
                delta = audio[self._stream_sent :]
                self._stream_sent = len(audio)
                speaking = now - last_speech < self.SPEECH_HOLD_SEC
                text = self.asr.push_stream(delta, decode=speaking) if len(delta) else ""
                settled = False
                if self._speech_since_refresh and now - last_speech >= self.PAUSE_REFRESH_SEC:
                    self._speech_since_refresh = False
                    text = self.asr.refresh_stream() or text
                    settled = True
            if text and (text != self.shown or settled):
                self._show_partial(text, settled)
            else:
                self._hud("level", level=self._ui_level())

    def _window_loop(self) -> None:
        """whisper / transformers 后端：滑窗重解码 + 连续两次的公共前缀作为预览。"""
        window_sec = QWEN_PARTIAL_WINDOW_SEC if self.config.backend.lower() in QWEN_BACKENDS else 0.0
        window_start = 0
        prev = ""
        tick = min(0.12, self.config.partial_interval)
        next_decode = time.time() + self.config.partial_interval
        while not self.session_stop.wait(tick):
            if time.time() < next_decode or not self.config.partial:
                self._hud("level", level=self._ui_level())
                continue
            next_decode = time.time() + self.config.partial_interval
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
                        prev = ""
                    audio = audio[start:]
            try:
                text = self._clean_partial(self.asr.transcribe(audio, partial=True))
            except Exception as exc:  # noqa: BLE001
                LOG.debug("partial ASR failed: %s", exc)
                continue
            if not text:
                continue
            prefix = self._common_prefix(prev, text) if prev else ""
            prev = text
            if len(prefix) >= 2:
                self._show_partial(prefix, settled=True)

    # ------------------------------------------------------------------ final

    def _worker(self) -> None:
        while True:
            item = self.final_queue.get()
            if item is None:
                return
            kind, payload, target = item
            if kind == "text":
                text = payload
            else:
                try:
                    # 兜底整段识别：补上 trim/normalize（stop 返回原始音频）
                    processed = normalize_audio(trim_silence(payload, self.config.sample_rate))
                    text = self.asr.transcribe(processed, partial=False)
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("ASR failed: %s", exc)
                    self._hud("error", target, text="识别失败")
                    continue
            if not text:
                LOG.info("ASR returned empty text")
                self._hud("hide", target)
                continue
            LOG.info("ASR: %s", text)
            target = target or self._resolve_target()
            if not target:
                LOG.error("insert skipped: no active target")
                continue
            try:
                result = self.send_client.request({"command": "send", "target": target, "text": text})
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            if result.get("ok"):
                LOG.info("inserted into %s", target)
            else:
                LOG.error("insert into %s failed: %s", target, result.get("error"))
                self._hud("error", target, text=f"未能输入：{result.get('error')}")

    # ------------------------------------------------------------------ lifecycle

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
        self.session_stop.set()
        self.final_queue.put(None)
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
                self._lock_handle.close()
            except OSError:
                pass
            self._lock_handle = None
