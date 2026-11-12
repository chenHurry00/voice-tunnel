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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="~/.cache/voice-tunnel/models/Qwen3-ASR-0.6B")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--backend", choices=["transformers", "vllm"], default="transformers")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.55)
    parser.add_argument("--chunk-size-sec", type=float, default=0.5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import numpy as np  # noqa: F401
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
            max_model_len=2048,
            max_new_tokens=256,
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
    LOG.info("Qwen3-ASR ready")

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
            with lock:
                results = model.transcribe(audio=(audio, sample_rate), language=language)
            result = results[0] if results else None
            text = getattr(result, "text", "") if result is not None else ""
            lang = getattr(result, "language", None) if result is not None else None
            return {"ok": True, "text": text, "language": lang}

        def _stream_start(self, payload: dict[str, Any]) -> dict[str, Any]:
            if args.backend != "vllm":
                raise RuntimeError("streaming requires the vllm backend")
            language = normalize_language(payload.get("language"))
            state = model.init_streaming_state(
                language=language,
                unfixed_chunk_num=4,
                unfixed_token_num=5,
                chunk_size_sec=args.chunk_size_sec,
            )
            session_id = uuid.uuid4().hex
            sessions[session_id] = {"state": state, "created": time.time()}
            LOG.info("stream start %s (language=%s)", session_id, language)
            return {"ok": True, "session_id": session_id}

        def _stream_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
            session_id = str(payload["session_id"])
            entry = sessions.get(session_id)
            if entry is None:
                raise RuntimeError(f"unknown session: {session_id}")
            final = bool(payload.get("final"))
            pcm = self._pcm(payload)
            with lock:
                if pcm.size:
                    model.streaming_transcribe(pcm, entry["state"])
                if final:
                    model.finish_streaming_transcribe(entry["state"])
                text = entry["state"].text or ""
                lang = entry["state"].language or None
            if final:
                sessions.pop(session_id, None)
                LOG.info("stream end %s: %s", session_id, text[:60])
            return {"ok": True, "text": text, "language": lang}

        def _stream_refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
            """停顿微调：不带前缀整段重解码，让前文随时被修正（豆包式）。"""
            session_id = str(payload["session_id"])
            entry = sessions.get(session_id)
            if entry is None:
                raise RuntimeError(f"unknown session: {session_id}")
            state = entry["state"]
            if state.buffer is not None and state.buffer.shape[0]:
                # 关键：把不足一个 chunk 的尾部并入 audio_accum，否则最后几个字不参与解码
                state.audio_accum = np.concatenate([state.audio_accum, state.buffer])
                state.buffer = np.zeros((0,), dtype=np.float32)
            if state.audio_accum.shape[0] == 0:
                return {"ok": True, "text": state.text or "", "language": state.language or None}
            with lock:
                inp = {"prompt": state.prompt_raw, "multi_modal_data": {"audio": [state.audio_accum]}}
                outputs = model.model.generate([inp], sampling_params=model.sampling_params, use_tqdm=False)
                state._raw_decoded = outputs[0].outputs[0].text
                lang, txt = parse_asr_output(state._raw_decoded, user_language=state.force_language)
                state.language = lang
                state.text = txt
            LOG.info("stream refresh %s: %s", session_id, txt[:60])
            return {"ok": True, "text": txt, "language": lang}

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
