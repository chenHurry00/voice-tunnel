#!/usr/bin/env python3
"""Voice Tunnel desktop GUI (PyQt5).

Run with the system Python that has python3-pyqt5 installed:
    python3 gui/voice_tunnel_gui.py
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PyQt5 import QtCore, QtGui, QtWidgets

APP_DIR = Path(__file__).resolve().parents[1]
CONFIG = APP_DIR / "config.ini"
CLI = APP_DIR / ".venv" / "bin" / "voice-tunnel"
DEFAULT_DATA_DIR = "~/.cache/voice-tunnel"
DEFAULT_PTT_DEVICE = "/dev/input/by-id/usb-ROYUAN_Eopmaker_Keyboard-event-kbd"
PTT_LABELS = {
    "KEY_RIGHTCTRL": "右 Ctrl",
    "KEY_LEFTCTRL": "左 Ctrl",
    "KEY_RIGHTALT": "右 Alt",
    "KEY_PAUSE": "Pause",
    "KEY_F13": "F13",
}


def pretty_key(name: str) -> str:
    return PTT_LABELS.get(name, name)


def list_models(backend: str = "whisper", data_dir: str = DEFAULT_DATA_DIR) -> list[tuple[str, str]]:
    root = Path(os.path.expanduser(data_dir)) / "models"
    result: list[tuple[str, str]] = []
    for directory in sorted(root.glob("*")):
        name = directory.name
        is_qwen = name.startswith("Qwen3-ASR-")
        if backend == "qwen3":
            if not is_qwen or not (directory / "model.safetensors").exists():
                continue
            label = "Qwen3-ASR " + name[len("Qwen3-ASR-") :]
        else:
            if is_qwen or not (directory / "model.bin").exists():
                continue
            if name.startswith("faster-whisper-"):
                label = "faster-whisper " + name[len("faster-whisper-") :]
            else:
                label = name
        result.append((label, str(directory)))
    return result


def make_icon(color: str) -> QtGui.QIcon:
    pix = QtGui.QPixmap(64, 64)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor(color))
    painter.drawEllipse(2, 2, 60, 60)
    pen = QtGui.QPen(QtGui.QColor("#ffffff"), 4, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.drawRoundedRect(QtCore.QRectF(26, 13, 12, 23), 6, 6)
    painter.drawArc(QtCore.QRectF(21, 23, 22, 22), 180 * 16, 180 * 16)
    painter.drawLine(32, 45, 32, 51)
    painter.drawLine(25, 51, 39, 51)
    painter.end()
    return QtGui.QIcon(pix)


def load_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    if CONFIG.exists():
        parser.read(CONFIG, encoding="utf-8")
    return parser


def list_mics() -> list[str]:
    names: list[str] = []
    try:
        output = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return names
    for line in output.splitlines():
        match = re.match(r"card \d+: ([^ ]+) \[([^\]]+)\], device \d+: ([^\[]+) \[", line)
        if match:
            names.append(match.group(2).strip())
    return sorted(set(names))


class StatusWorker(QtCore.QThread):
    updated = QtCore.pyqtSignal(dict)

    def __init__(self, cli: Path, config: Path) -> None:
        super().__init__()
        self.cli = cli
        self.config = config
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            data: dict[str, Any] = {}
            try:
                result = subprocess.run(
                    [str(self.cli), "--config", str(self.config), "status"],
                    capture_output=True,
                    text=True,
                    timeout=6,
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout)
                else:
                    data = {"error": (result.stderr or "status failed").strip()}
            except Exception as exc:  # noqa: BLE001
                data = {"error": str(exc)}
            self.updated.emit(data)
            for _ in range(30):
                if self._stop:
                    return
                time.sleep(0.1)

    def stop(self) -> None:
        self._stop = True


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Voice Tunnel")
        self.setMinimumSize(860, 560)
        self.resize(980, 640)
        self.setWindowIcon(make_icon("#2563eb"))
        self.settings = QtCore.QSettings("voice-tunnel", "gui")
        self.proc: QtCore.QProcess | None = None
        self.worker: StatusWorker | None = None
        self._closing = False
        self._tray_hint_shown = False
        self.keytest_proc: QtCore.QProcess | None = None
        self.keytest_dialog: QtWidgets.QDialog | None = None
        self.keytest_buffer = ""
        self.keytest_timer: QtCore.QTimer | None = None
        self._build_ui()
        self._build_tray()
        self._load_config()
        self._start_status_worker()
        self._refresh_mics()
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = QtWidgets.QWidget(objectName="Root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        titles = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("Voice Tunnel")
        title.setObjectName("Title")
        subtitle = QtWidgets.QLabel("多主机语音输入 · X99 语音主机")
        subtitle.setObjectName("Subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        self.badge = QtWidgets.QLabel("已停止")
        self.badge.setObjectName("Badge")
        self.badge.setAlignment(QtCore.Qt.AlignCenter)
        self.badge.setFixedHeight(30)
        self.badge.setMinimumWidth(82)
        self.badge.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        header.addWidget(self.badge)
        outer.addLayout(header)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(14)
        outer.addLayout(body, 1)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(12)
        body.addLayout(left, 0)

        settings_box = QtWidgets.QGroupBox("语音设置")
        settings_box.setMinimumWidth(390)
        settings_box.setMaximumWidth(430)
        form = QtWidgets.QFormLayout(settings_box)
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(9)

        self.ptt_key = "KEY_RIGHTCTRL"
        self.ptt_button = QtWidgets.QPushButton(pretty_key(self.ptt_key))
        self.ptt_button.setObjectName("Secondary")
        self.ptt_button.setToolTip("点击后按下要作为 PTT 的键")
        self.ptt_button.clicked.connect(self._capture_ptt_key)
        form.addRow("PTT 键", self.ptt_button)

        mic_row = QtWidgets.QHBoxLayout()
        self.mic_combo = QtWidgets.QComboBox()
        self.mic_combo.setEditable(False)
        mic_refresh = QtWidgets.QPushButton("刷新")
        mic_refresh.setObjectName("Secondary")
        mic_refresh.clicked.connect(self._refresh_mics)
        mic_row.addWidget(self.mic_combo, 1)
        mic_row.addWidget(mic_refresh)
        form.addRow("麦克风", mic_row)

        self.backend_combo = QtWidgets.QComboBox()
        self.backend_combo.addItems(["faster-whisper", "Qwen3-ASR", "Qwen3-ASR (vLLM 流式)"])
        self.backend_combo.currentTextChanged.connect(self._backend_changed)
        form.addRow("识别后端", self.backend_combo)

        data_row = QtWidgets.QHBoxLayout()
        self.data_edit = QtWidgets.QLineEdit()
        self.data_edit.setPlaceholderText("模型/环境/日志的存放目录")
        self.data_edit.editingFinished.connect(lambda: self._refresh_models())
        data_browse = QtWidgets.QPushButton("浏览")
        data_browse.setObjectName("Secondary")
        data_browse.clicked.connect(self._pick_data_dir)
        data_row.addWidget(self.data_edit, 1)
        data_row.addWidget(data_browse)
        form.addRow("安装位置", data_row)

        model_row = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setEditable(False)
        model_refresh = QtWidgets.QPushButton("刷新")
        model_refresh.setObjectName("Secondary")
        model_refresh.clicked.connect(self._refresh_models)
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(model_refresh)
        form.addRow("识别模型", model_row)

        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.addItems(["GPU (float16)", "CPU (int8)"])
        form.addRow("运行设备", self.device_combo)

        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.addItem("自动（当前屏幕）")
        form.addRow("目标主机", self.target_combo)

        preview_row = QtWidgets.QHBoxLayout()
        self.partial_check = QtWidgets.QCheckBox("说话中预览")
        self.partial_check.setChecked(True)
        self.interval_spin = QtWidgets.QDoubleSpinBox()
        self.interval_spin.setRange(0.3, 2.0)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" s")
        preview_row.addWidget(self.partial_check)
        preview_row.addStretch(1)
        preview_row.addWidget(self.interval_spin)
        form.addRow("预览刷新", preview_row)

        left.addWidget(settings_box)

        buttons = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("保存并应用")
        self.save_btn.setObjectName("Secondary")
        self.save_btn.clicked.connect(self._save_clicked)
        self.start_btn = QtWidgets.QPushButton("开始监听")
        self.start_btn.clicked.connect(self.start_listener)
        self.stop_btn = QtWidgets.QPushButton("停止监听")
        self.stop_btn.setObjectName("Secondary")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_listener)
        self.start_btn.setToolTip("开始监听（Ctrl+R）")
        self.stop_btn.setToolTip("停止监听（Ctrl+T）")
        buttons.addWidget(self.save_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        left.addLayout(buttons)

        start_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+R"), self)
        start_shortcut.activated.connect(self.start_listener)
        stop_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+T"), self)
        stop_shortcut.activated.connect(self.stop_listener)
        left.addStretch(1)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 1)

        log_box = QtWidgets.QGroupBox("运行日志")
        log_layout = QtWidgets.QVBoxLayout(log_box)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log)
        right.addWidget(log_box, 1)

        footer = QtWidgets.QHBoxLayout()
        self.target_label = QtWidgets.QLabel("目标：yuchen-X99")
        self.agents_label = QtWidgets.QLabel("Agents：—")
        self.target_label.setObjectName("Footer")
        self.agents_label.setObjectName("Footer")
        footer.addWidget(self.target_label)
        footer.addStretch(1)
        footer.addWidget(self.agents_label)
        right.addLayout(footer)

        self.setStyleSheet(
            """
            QWidget { font-family: "Noto Sans CJK SC", "Microsoft YaHei UI"; font-size: 10.5pt; color: #1f2937; }
            QWidget#Root { background: #f4f6fb; }
            QLabel#Title { font-size: 21pt; font-weight: 700; color: #111827; }
            QLabel#Subtitle { color: #6b7280; font-size: 9.5pt; }
            QLabel#Badge { background: #e5e7eb; color: #4b5563; border-radius: 15px; padding: 0 12px; font-weight: 600; }
            QLabel#Footer { color: #6b7280; font-size: 9pt; }
            QGroupBox { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; margin-top: 12px; padding: 14px 12px 12px 12px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: #2563eb; }
            QLineEdit, QComboBox, QDoubleSpinBox { background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px; padding: 5px 8px; min-height: 24px; }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border: 1px solid #2563eb; }
            QComboBox::drop-down { border: none; width: 22px; }
            QPushButton { background: #2563eb; color: #ffffff; border: none; border-radius: 6px; padding: 7px 14px; font-weight: 600; }
            QPushButton:hover { background: #1d4ed8; }
            QPushButton:disabled { background: #c7d2fe; color: #eef2ff; }
            QPushButton#Secondary { background: #eef2ff; color: #1d4ed8; }
            QPushButton#Secondary:hover { background: #e0e7ff; }
            QPlainTextEdit { background: #0f172a; color: #dbeafe; border: none; border-radius: 8px; padding: 8px; font-family: "JetBrains Mono", "Noto Sans Mono", monospace; font-size: 9.5pt; }
            QCheckBox { spacing: 6px; }
            """
        )

    def _build_tray(self) -> None:
        self.tray = QtWidgets.QSystemTrayIcon(make_icon("#9ca3af"), self)
        menu = QtWidgets.QMenu()
        self.tray_toggle = menu.addAction("显示主窗口")
        self.tray_start = menu.addAction("开始监听")
        self.tray_stop = menu.addAction("停止监听")
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        self.tray_toggle.triggered.connect(self.toggle_window)
        self.tray_start.triggered.connect(self.start_listener)
        self.tray_stop.triggered.connect(self.stop_listener)
        quit_action.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip("Voice Tunnel · 已停止")
        self.tray.show()
        self.tray_stop.setEnabled(False)

    # ------------------------------------------------------------------ config

    def _load_config(self) -> None:
        parser = load_parser()
        section = parser["voice"] if parser.has_section("voice") else {}
        self.ptt_key = section.get("ptt_key", "KEY_RIGHTCTRL")
        self.ptt_button.setText(pretty_key(self.ptt_key))
        self.mic_combo.setCurrentText(section.get("device", "USBZH5XK"))
        self.data_edit.setText(section.get("data_dir", DEFAULT_DATA_DIR))
        backend = section.get("backend", "whisper").lower()
        qwen_backend = section.get("qwen_backend", "transformers").lower()
        self.backend_combo.blockSignals(True)
        if backend in {"qwen3", "qwen", "qwen3-asr"}:
            self.backend_combo.setCurrentText("Qwen3-ASR (vLLM 流式)" if qwen_backend == "vllm" else "Qwen3-ASR")
        else:
            self.backend_combo.setCurrentText("faster-whisper")
        self.backend_combo.blockSignals(False)
        default_model = self._default_model("qwen3" if backend in {"qwen3", "qwen", "qwen3-asr"} else "whisper")
        self.model_path = os.path.expanduser(section.get("model", default_model))
        self._refresh_models()
        device_type = section.get("device_type", "cuda")
        self.device_combo.setCurrentText("CPU (int8)" if device_type == "cpu" else "GPU (float16)")
        self.partial_check.setChecked(section.get("partial", "true").lower() in {"1", "true", "yes", "on"})
        self.interval_spin.setValue(float(section.get("partial_interval", "0.5")))
        target = section.get("target", "active")
        self._set_target_selection(target)

    def _backend_kind(self) -> tuple[str, str]:
        text = self.backend_combo.currentText()
        if text.startswith("Qwen3"):
            return "qwen3", ("vllm" if "vLLM" in text else "transformers")
        return "whisper", "transformers"

    def _default_model(self, backend: str) -> str:
        name = "Qwen3-ASR-0.6B" if backend == "qwen3" else "faster-whisper-small"
        return str(Path(os.path.expanduser(self.data_edit.text().strip() or DEFAULT_DATA_DIR)) / "models" / name)

    def _pick_data_dir(self) -> None:
        current = os.path.expanduser(self.data_edit.text().strip() or DEFAULT_DATA_DIR)
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择安装位置", current)
        if path:
            self.data_edit.setText(path)
            self._refresh_models()

    def _set_target_selection(self, target: str) -> None:
        if target and target != "active":
            index = self.target_combo.findText(target)
            if index < 0:
                self.target_combo.addItem(target)
                index = self.target_combo.findText(target)
            self.target_combo.setCurrentIndex(index)
        else:
            self.target_combo.setCurrentIndex(0)

    def _save_config(self) -> bool:
        parser = load_parser()
        if not parser.has_section("voice"):
            parser.add_section("voice")
        section = parser["voice"]
        section["device"] = self.mic_combo.currentText().strip()
        backend, qwen_backend = self._backend_kind()
        section["backend"] = backend
        section["qwen_backend"] = qwen_backend
        section["qwen_url"] = (
            "http://127.0.0.1:8771/transcribe" if qwen_backend == "vllm" else "http://127.0.0.1:8770/transcribe"
        )
        section["data_dir"] = self.data_edit.text().strip() or DEFAULT_DATA_DIR
        section["ptt_key"] = self.ptt_key
        if "ptt_device" not in section or not section.get("ptt_device", "").strip():
            section["ptt_device"] = DEFAULT_PTT_DEVICE
        model = self.model_combo.currentData() or self._default_model(backend)
        section["model"] = model
        section["partial_model"] = model
        gpu = self.device_combo.currentText().startswith("GPU")
        section["device_type"] = "cuda" if gpu else "cpu"
        section["compute_type"] = "float16" if gpu else "int8"
        section["partial"] = "true" if self.partial_check.isChecked() else "false"
        section["partial_interval"] = f"{self.interval_spin.value():.1f}"
        section["target"] = "active" if self.target_combo.currentIndex() == 0 else self.target_combo.currentText()
        section["min_seconds"] = section.get("min_seconds", "0.3")
        section["max_seconds"] = section.get("max_seconds", "30")
        try:
            with CONFIG.open("w", encoding="utf-8") as handle:
                parser.write(handle)
        except OSError as exc:
            self._append(f"保存配置失败：{exc}")
            QtWidgets.QMessageBox.warning(self, "Voice Tunnel", f"保存配置失败：{exc}")
            return False
        self._append("设置已保存")
        return True

    def _backend_changed(self, _text: str) -> None:
        if not hasattr(self, "model_path") or not hasattr(self, "model_combo"):
            return
        backend, qwen_backend = self._backend_kind()
        self.model_path = self._default_model(backend)
        self.interval_spin.setValue(0.5 if qwen_backend == "vllm" else (0.8 if backend == "qwen3" else 0.5))
        self._refresh_models()

    def _refresh_models(self) -> None:
        backend, _qwen_backend = self._backend_kind()
        default_model = self._default_model(backend)
        current = getattr(self, "model_path", default_model)
        models = list_models(backend, self.data_edit.text().strip() or DEFAULT_DATA_DIR)
        if not models:
            models = [("Qwen3-ASR 0.6B（默认）" if backend == "qwen3" else "faster-whisper small（默认）", default_model)]
        current_path = os.path.expanduser(current)
        if not any(os.path.expanduser(path) == current_path for _label, path in models):
            models.insert(0, (Path(current_path).name or "当前模型", current))
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for label, path in models:
            self.model_combo.addItem(label, path)
        index = self.model_combo.findData(current)
        if index < 0:
            index = self.model_combo.findData(os.path.expanduser(default_model))
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.blockSignals(False)

    def _capture_ptt_key(self) -> None:
        if not CLI.exists():
            QtWidgets.QMessageBox.critical(self, "Voice Tunnel", f"找不到 {CLI}")
            return
        if self.keytest_proc is not None and self.keytest_proc.state() != QtCore.QProcess.NotRunning:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("设置 PTT 键")
        dialog.setModal(True)
        dialog.setFixedSize(360, 120)
        layout = QtWidgets.QVBoxLayout(dialog)
        label = QtWidgets.QLabel("请按下要作为 PTT 的按键（12 秒内）")
        label.setAlignment(QtCore.Qt.AlignCenter)
        hint = QtWidgets.QLabel("推荐：右 Ctrl / Pause / F13")
        hint.setAlignment(QtCore.Qt.AlignCenter)
        hint.setStyleSheet("color:#6b7280;")
        layout.addWidget(label)
        layout.addWidget(hint)
        dialog.rejected.connect(lambda: self._cancel_keytest(False, dialog))
        dialog.show()
        self.keytest_dialog = dialog
        self.keytest_buffer = ""
        self.keytest_proc = QtCore.QProcess(self)
        self.keytest_proc.setProgram(str(CLI))
        self.keytest_proc.setArguments(["--config", str(CONFIG), "keytest", "--once"])
        self.keytest_proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.keytest_proc.readyReadStandardOutput.connect(self._read_keytest)
        self.keytest_proc.finished.connect(self._keytest_finished)
        self.keytest_proc.start()
        self.keytest_timer = QtCore.QTimer(self)
        self.keytest_timer.setSingleShot(True)
        self.keytest_timer.timeout.connect(lambda: self._cancel_keytest(True, dialog))
        self.keytest_timer.start(12000)

    def _read_keytest(self) -> None:
        if self.keytest_proc is None:
            return
        self.keytest_buffer += bytes(self.keytest_proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        match = re.search(r"\b(KEY_[A-Z0-9_]+) \(code=", self.keytest_buffer)
        if match:
            self.ptt_key = match.group(1)
            self.ptt_button.setText(pretty_key(self.ptt_key))
            self._append(f"PTT 键已设为 {pretty_key(self.ptt_key)}，点击“保存并应用”生效")
            self._finish_keytest()

    def _finish_keytest(self) -> None:
        if self.keytest_timer is not None:
            self.keytest_timer.stop()
            self.keytest_timer = None
        if self.keytest_dialog is not None:
            self.keytest_dialog.close()
            self.keytest_dialog = None
        if self.keytest_proc is not None and self.keytest_proc.state() != QtCore.QProcess.NotRunning:
            self.keytest_proc.kill()
        self._save_config()

    def _cancel_keytest(self, notify: bool, dialog: QtWidgets.QDialog | None = None) -> None:
        if self.keytest_timer is not None:
            self.keytest_timer.stop()
            self.keytest_timer = None
        if self.keytest_proc is not None and self.keytest_proc.state() != QtCore.QProcess.NotRunning:
            self.keytest_proc.kill()
        if dialog is not None:
            dialog.close()
        self.keytest_dialog = None
        if notify:
            QtWidgets.QMessageBox.information(self, "Voice Tunnel", "没有检测到按键，PTT 键未修改。")

    def _keytest_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        if self.keytest_dialog is not None:
            self._cancel_keytest(False, self.keytest_dialog)

    def _refresh_mics(self) -> None:
        current = self.mic_combo.currentText().strip()
        names = ["默认输入"] + list_mics()
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItems(names)
        if current and current in names:
            self.mic_combo.setCurrentText(current)
        elif "USBZH5XK" in names:
            self.mic_combo.setCurrentText("USBZH5XK")
        self.mic_combo.blockSignals(False)

    # ------------------------------------------------------------------ process

    def start_listener(self) -> None:
        if not CLI.exists():
            QtWidgets.QMessageBox.critical(self, "Voice Tunnel", f"找不到 {CLI}\n请先安装 .venv 和语音依赖。")
            return
        if self.proc is not None and self.proc.state() != QtCore.QProcess.NotRunning:
            self.stop_listener()
            time.sleep(0.3)
        if not self._save_config():
            return
        self.log.clear()
        self._append("启动监听…")
        self.proc = QtCore.QProcess(self)
        self.proc.setProgram(str(CLI))
        self.proc.setArguments(["--config", str(CONFIG), "listen"])
        self.proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read_process_output)
        self.proc.finished.connect(self._process_finished)
        self.proc.errorOccurred.connect(self._process_error)
        self.proc.start()
        self._set_running(False, "加载中", "#f59e0b")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def stop_listener(self) -> None:
        if self.proc is None or self.proc.state() == QtCore.QProcess.NotRunning:
            self._set_running(False)
            return
        self._append("停止监听…")
        self.proc.terminate()
        if not self.proc.waitForFinished(2500):
            self.proc.kill()
            self.proc.waitForFinished(1000)
        self._set_running(False)

    def _read_process_output(self) -> None:
        if self.proc is None:
            return
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._append(text.rstrip())
        if "voice listener ready" in text:
            self._set_running(True, "运行中", "#16a34a")
        if "another voice-tunnel listener" in text:
            self._append("已有监听器在运行，请先停止旧的监听器。")
            self._set_running(False, "已停止", "#9ca3af")

    def _process_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        self._append(f"监听进程已退出（code={code}）")
        self._set_running(False)

    def _process_error(self, error: QtCore.QProcess.ProcessError) -> None:
        self._append(f"监听进程错误：{error}")
        self._set_running(False)

    def _set_running(self, running: bool, text: str | None = None, color: str | None = None) -> None:
        if running:
            label, color = "运行中", "#dcfce7"
            text_color = "#166534"
        else:
            label = text or "已停止"
            color = color or "#e5e7eb"
            text_color = "#4b5563"
        self.badge.setText(label)
        self.badge.setStyleSheet(
            f"background:{color}; color:{text_color}; border-radius:11px; padding:4px 12px; font-weight:600;"
        )
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.tray_start.setEnabled(not running)
        self.tray_stop.setEnabled(running)
        self.tray.setIcon(make_icon("#16a34a" if running else "#9ca3af"))
        self.tray.setToolTip("Voice Tunnel · " + ("运行中" if running else label))

    # ------------------------------------------------------------------ status

    def _start_status_worker(self) -> None:
        if not CLI.exists():
            return
        self.worker = StatusWorker(CLI, CONFIG)
        self.worker.updated.connect(self._on_status)
        self.worker.start()

    def _on_status(self, data: dict[str, Any]) -> None:
        if data.get("error"):
            self.target_label.setText("目标：服务端未连接")
            self.agents_label.setText("Agents：0")
            return
        active = data.get("active_target") or "未知"
        default = data.get("default_target")
        self.target_label.setText(f"目标：{active}" + (f"（默认 {default}）" if default and active == default else ""))
        agents = data.get("agents") or []
        self.agents_label.setText(f"Agents：{len(agents)}")
        current = self.target_combo.currentText()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem("自动（当前屏幕）")
        for agent in agents:
            host = agent.get("host")
            if host:
                self.target_combo.addItem(host)
        if current and current != "自动（当前屏幕）":
            index = self.target_combo.findText(current)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)

    # ------------------------------------------------------------------ window / tray

    def _append(self, text: str) -> None:
        if not text:
            return
        self.log.appendPlainText(text)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _save_clicked(self) -> None:
        if not self._save_config():
            return
        if self.proc is not None and self.proc.state() != QtCore.QProcess.NotRunning:
            self.stop_listener()
            QtCore.QTimer.singleShot(300, self.start_listener)

    def _tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QtWidgets.QSystemTrayIcon.Trigger, QtWidgets.QSystemTrayIcon.DoubleClick):
            self.toggle_window()

    def toggle_window(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        if hasattr(self, "tray_toggle"):
            self.tray_toggle.setText("隐藏主窗口")

    def hideEvent(self, event: QtGui.QHideEvent) -> None:  # noqa: N802
        super().hideEvent(event)
        if hasattr(self, "tray_toggle"):
            self.tray_toggle.setText("显示主窗口")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self._closing:
            event.accept()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage("Voice Tunnel", "已最小化到右上角托盘；可从托盘菜单退出。")
        else:
            self.quit_app()

    def quit_app(self) -> None:
        self._closing = True
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
        self.stop_listener()
        self.tray.hide()
        QtWidgets.QApplication.quit()


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Voice Tunnel")
    app.setDesktopFileName("voice-tunnel")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if "--start" in sys.argv:
        QtCore.QTimer.singleShot(400, window.start_listener)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
