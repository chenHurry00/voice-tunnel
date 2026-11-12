#!/usr/bin/env python3
"""Local Qwen3-ASR HTTP service (run with .venv-qwen).

    .venv-qwen/bin/python scripts/qwen_asr_service.py \
        --model ~/.cache/voice-tunnel/models/Qwen3-ASR-0.6B --port 8770
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOG = logging.getLogger("qwen_asr_service")

SAMPLE_RATE = 16000
STREAM_MIN_AUDIO_SEC = 0.3   # 不足 0.3s 的开头不解码（只会出幻觉）
STREAM_MIN_NEW_SEC = 0.1     # 新增音频不足 0.1s 不重解码
STREAM_PREFIX_AFTER_SEC = 1.5  # 前 1.5s 不用前缀（早期结果不稳定）
STREAM_ROLLBACK_TOKENS = 5   # 用上次结果做前缀时回滚末尾 5 个 token，让尾部可被修正
SESSION_TTL_SEC = 900
SEGMENT_SEC = 20.0  # 停顿校正时音频已超过这么长，就把校正结果定稿，后续只解码新的一段

# Qwen3-ASR 只接受规范语言名（Chinese/English/...），这里兼容 ISO 代码。
LANGUAGE_ALIASES = {
    "zh": "Chinese",
    "zh-cn": "Chinese",
    "zh-hans": "Chinese",
    "cmn": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "ru": "Russian",
    "it": "Italian",
    "pt": "Portuguese",
    "ar": "Arabic",
}


def normalize_language(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"auto", "none"}:
        return None
    return LANGUAGE_ALIASES.get(text.lower(), text[:1].upper() + text[1:].lower())


def echoes_context(text: str, context: str) -> bool:
    """静音/底噪时模型会把系统提示词原样吐出来，这不是识别结果（长度门槛避免误伤说出的短词）。"""
    text = "".join(text.split())
    context = "".join(context.split())
    return len(text) >= 8 and text[:8] in context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="~/.cache/voice-tunnel/models/Qwen3-ASR-0.6B")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--backend", choices=["transformers", "vllm"], default="transformers")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.55)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import numpy as np
    import os
    import sys

    # torch.compile / inductor 调用 ninja 时依赖 PATH
    os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

    import torch
    from qwen_asr import Qwen3ASRModel
    from qwen_asr.inference.utils import parse_asr_output

    model_path = os.path.expanduser(args.model)
    LOG.info("loading Qwen3-ASR from %s (backend=%s)", model_path, args.backend)
    if args.backend == "vllm":
        model = Qwen3ASRModel.LLM(
            model=model_path,
            gpu_memory_utilization=args.gpu_memory_utilization,
            max_model_len=4096,
            max_new_tokens=512,
            dtype="float16",
        )
    else:
        model = Qwen3ASRModel.from_pretrained(
            model_path,
            dtype=torch.float16,
            device_map=args.device,
            max_inference_batch_size=1,
            max_new_tokens=512,
        )
    lock = threading.Lock()
    sessions: dict[str, dict[str, Any]] = {}
    # 预热：首次 generate 会触发 CUDA graph/编译（实测 4s+），放在启动阶段而不是第一次按键
    warm = time.time()
    model.transcribe(audio=(np.zeros(SAMPLE_RATE, dtype=np.float32), SAMPLE_RATE), language="Chinese")
    LOG.info("Qwen3-ASR ready (warmup %.1fs)", time.time() - warm)

    class Handler(BaseHTTPRequestHandler):
        server: ThreadingHTTPServer

        def log_message(self, fmt: str, *a: Any) -> None:  # noqa: A003
            LOG.debug(fmt, *a)

        def _json(self, code: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._json(200, {"ok": True, "model": model_path, "backend": args.backend})
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _pcm(self, payload: dict[str, Any]) -> Any:
            return np.frombuffer(base64.b64decode(payload["audio"]), dtype=np.float32)

        def do_POST(self) -> None:  # noqa: N802
            routes = {
                "/transcribe": self._transcribe,
                "/stream/start": self._stream_start,
                "/stream/chunk": self._stream_chunk,
                "/stream/refresh": self._stream_refresh,
                "/stream/cancel": self._stream_cancel,
            }
            handler = routes.get(self.path)
            if handler is None:
                self._json(404, {"ok": False, "error": "not found"})
                return
            try:
                self._json(200, handler(self._read_json()))
            except KeyError as exc:
                self._json(400, {"ok": False, "error": f"missing field: {exc}"})
            except Exception as exc:  # noqa: BLE001
                LOG.exception("%s failed", self.path)
                self._json(500, {"ok": False, "error": str(exc)})

        def _transcribe(self, payload: dict[str, Any]) -> dict[str, Any]:
            audio = self._pcm(payload)
            sample_rate = int(payload.get("sample_rate", 16000))
            language = normalize_language(payload.get("language"))
            context = str(payload.get("context") or "")
            with lock:
                results = model.transcribe(audio=(audio, sample_rate), context=context, language=language)
            result = results[0] if results else None
            text = getattr(result, "text", "") if result is not None else ""
            if echoes_context(text, context):
                text = ""
            lang = getattr(result, "language", None) if result is not None else None
            return {"ok": True, "text": text, "language": lang}

        def _stream_start(self, payload: dict[str, Any]) -> dict[str, Any]:
            if args.backend != "vllm":
                raise RuntimeError("streaming requires the vllm backend")
            language = normalize_language(payload.get("language"))
            # 只借用库的 prompt 构造；增量解码由本服务自己调度（见 _stream_chunk）
            context = str(payload.get("context") or "")
            state = model.init_streaming_state(context=context, language=language)
            now = time.time()
            for sid in [k for k, v in sessions.items() if now - v["created"] > SESSION_TTL_SEC]:
                sessions.pop(sid, None)
            session_id = uuid.uuid4().hex
            sessions[session_id] = {"state": state, "created": now, "decoded": 0, "committed": "", "context": context}
            LOG.info("stream start %s (language=%s)", session_id, language)
            return {"ok": True, "session_id": session_id}

        def _session(self, payload: dict[str, Any]) -> dict[str, Any]:
            entry = sessions.get(str(payload["session_id"]))
            if entry is None:
                raise RuntimeError(f"unknown session: {payload['session_id']}")
            return entry

        def _rollback_prefix(self, raw: str) -> str:
            tokenizer = model.processor.tokenizer
            ids = tokenizer.encode(raw)
            k = STREAM_ROLLBACK_TOKENS
            while True:
                end_idx = len(ids) - k
                if end_idx <= 0:
                    return ""
                prefix = tokenizer.decode(ids[:end_idx])
                if "\ufffd" not in prefix:  # 不能从多字节字符中间截断
                    return prefix
                k += 1

        def _decode(self, entry: dict[str, Any], use_prefix: bool) -> None:
            state = entry["state"]
            audio = state.audio_accum
            prefix = self._rollback_prefix(state._raw_decoded) if use_prefix and state._raw_decoded else ""
            inp = {"prompt": state.prompt_raw + prefix, "multi_modal_data": {"audio": [audio]}}
            outputs = model.model.generate([inp], sampling_params=model.sampling_params, use_tqdm=False)
            state._raw_decoded = prefix + outputs[0].outputs[0].text
            state.language, state.text = parse_asr_output(state._raw_decoded, user_language=state.force_language)
            if echoes_context(state.text, entry["context"]):
                state._raw_decoded, state.text = "", ""
            entry["decoded"] = audio.shape[0]

        def _stream_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
            """追加音频并（按需）做一次增量解码。

            与库自带的 streaming_transcribe 不同：不按固定块长切分、不逐块串行，
            每次请求把新到的音频全部并入后只解一次——解码慢时自动合并，延迟不累积。
            """
            entry = self._session(payload)
            state = entry["state"]
            pcm = self._pcm(payload)
            with lock:
                if pcm.size:
                    state.audio_accum = np.concatenate([state.audio_accum, pcm])
                total = state.audio_accum.shape[0]
                fresh = total - entry["decoded"]
                if (
                    payload.get("decode", True)
                    and total >= STREAM_MIN_AUDIO_SEC * SAMPLE_RATE
                    and fresh >= STREAM_MIN_NEW_SEC * SAMPLE_RATE
                ):
                    self._decode(entry, use_prefix=total >= STREAM_PREFIX_AFTER_SEC * SAMPLE_RATE)
                return {"ok": True, "text": self._full_text(entry), "language": state.language or None}

        @staticmethod
        def _join(left: str, right: str) -> str:
            if left and right and left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
                return left + " " + right
            return left + right

        def _full_text(self, entry: dict[str, Any]) -> str:
            return self._join(entry["committed"], entry["state"].text or "")

        def _stream_refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
            """停顿/松手微调：不带前缀整段重解码，让前文随时被修正。"""
            entry = self._session(payload)
            state = entry["state"]
            with lock:
                if state.audio_accum.shape[0]:
                    self._decode(entry, use_prefix=False)
                if state.audio_accum.shape[0] >= SEGMENT_SEC * SAMPLE_RATE:
                    # 长句分段：停顿处定稿，避免整段重解码越来越慢、超出生成长度
                    entry["committed"] = self._full_text(entry)
                    state.audio_accum = np.zeros((0,), dtype=np.float32)
                    state._raw_decoded, state.text, entry["decoded"] = "", "", 0
                text = self._full_text(entry)
            LOG.info("stream refresh %s: %s", payload["session_id"], text[-60:])
            return {"ok": True, "text": text, "language": state.language or None}

        def _stream_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
            sessions.pop(str(payload.get("session_id", "")), None)
            return {"ok": True}

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
