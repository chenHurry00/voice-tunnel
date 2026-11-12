#!/usr/bin/env python3
"""Voice Tunnel desktop GUI (PyQt5).

Run with the system Python that has python3-pyqt5 installed:
    python3 gui/voice_tunnel_gui.py
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PyQt5 import QtCore, QtGui, QtWidgets

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "src"))
from voice_tunnel.control import ControlClient  # noqa: E402  (stdlib-only，可在系统 Python 下导入)

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

    def __init__(self, config: Path) -> None:
        super().__init__()
        self.client = ControlClient(str(config), timeout=2)
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                result = self.client.request({"command": "status"})
                data = result.get("data") if result.get("ok") else {"error": result.get("error")}
            except Exception as exc:  # noqa: BLE001
                data = {"error": str(exc)}
            self.updated.emit(data or {})
            for _ in range(15):
                if self._stop:
                    break
                time.sleep(0.1)
        self.client.close()

    def stop(self) -> None:
        self._stop = True


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Voice Tunnel")
        self.setMinimumSize(780, 480)
        self.resize(860, 540)
        self.setWindowIcon(make_icon("#1f2329"))
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
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        # 状态行：圆点 + 状态 + 当前目标
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        self.status_dot = QtWidgets.QLabel(objectName="Dot")
        self.status_dot.setFixedSize(8, 8)
        self.status_label = QtWidgets.QLabel("已停止", objectName="Status")
        self.target_label = QtWidgets.QLabel("", objectName="Muted")
        header.addWidget(self.status_dot)
        header.addWidget(self.status_label)
        header.addStretch(1)
        header.addWidget(self.target_label)
        outer.addLayout(header)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(14)
        outer.addLayout(body, 1)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(10)
        body.addLayout(left, 0)

        form_box = QtWidgets.QFrame(objectName="Panel")
        form_box.setFixedWidth(360)
        form = QtWidgets.QFormLayout(form_box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.ptt_key = "KEY_RIGHTCTRL"
        self.ptt_button = QtWidgets.QPushButton(pretty_key(self.ptt_key))
        self.ptt_button.setToolTip("点击后按下要作为按住说话的键")
        self.ptt_button.clicked.connect(self._capture_ptt_key)
        form.addRow("说话键", self.ptt_button)

        mic_row = QtWidgets.QHBoxLayout()
        self.mic_combo = QtWidgets.QComboBox()
        mic_refresh = QtWidgets.QToolButton(text="刷新")
        mic_refresh.clicked.connect(self._refresh_mics)
        mic_row.addWidget(self.mic_combo, 1)
        mic_row.addWidget(mic_refresh)
        form.addRow("麦克风", mic_row)

        self.backend_combo = QtWidgets.QComboBox()
        self.backend_combo.addItems(["faster-whisper", "Qwen3-ASR", "Qwen3-ASR (vLLM 流式)"])
        self.backend_combo.currentTextChanged.connect(self._backend_changed)
        form.addRow("识别后端", self.backend_combo)

        model_row = QtWidgets.QHBoxLayout()
        self.model_combo = QtWidgets.QComboBox()
        model_refresh = QtWidgets.QToolButton(text="刷新")
        model_refresh.clicked.connect(self._refresh_models)
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(model_refresh)
        form.addRow("模型", model_row)

        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.addItems(["GPU (float16)", "CPU (int8)"])
        form.addRow("运行设备", self.device_combo)

        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.addItem("自动（当前屏幕）")
        form.addRow("目标主机", self.target_combo)

        preview_row = QtWidgets.QHBoxLayout()
        self.partial_check = QtWidgets.QCheckBox("边说边显示")
        self.partial_check.setChecked(True)
        self.interval_spin = QtWidgets.QDoubleSpinBox()
        self.interval_spin.setRange(0.3, 2.0)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.interval_spin.setFixedWidth(64)
        preview_row.addWidget(self.partial_check)
        preview_row.addStretch(1)
        preview_row.addWidget(self.interval_spin)
        form.addRow("实时字幕", preview_row)

        data_row = QtWidgets.QHBoxLayout()
        self.data_edit = QtWidgets.QLineEdit()
        self.data_edit.setPlaceholderText("模型 / 环境 / 日志目录")
        self.data_edit.editingFinished.connect(lambda: self._refresh_models())
        data_browse = QtWidgets.QToolButton(text="…")
        data_browse.clicked.connect(self._pick_data_dir)
        data_row.addWidget(self.data_edit, 1)
        data_row.addWidget(data_browse)
        form.addRow("数据目录", data_row)
        left.addWidget(form_box)

        buttons = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("保存并应用")
        self.save_btn.clicked.connect(self._save_clicked)
        self.run_btn = QtWidgets.QPushButton("开始监听", objectName="Primary")
        self.run_btn.setToolTip("开始 Ctrl+R / 停止 Ctrl+T")
        self.run_btn.clicked.connect(lambda: self.stop_listener() if self._running_or_loading() else self.start_listener())
        buttons.addWidget(self.save_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.run_btn)
        left.addLayout(buttons)
        left.addStretch(1)

        start_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+R"), self)
        start_shortcut.activated.connect(self.start_listener)
        stop_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+T"), self)
        stop_shortcut.activated.connect(self.stop_listener)

        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        body.addLayout(right, 1)

        right.addWidget(QtWidgets.QLabel("主机", objectName="Section"))
        self.hosts = QtWidgets.QTreeWidget(objectName="Panel")
        self.hosts.setColumnCount(2)
        self.hosts.setHeaderHidden(True)
        self.hosts.setRootIsDecorated(False)
        self.hosts.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.hosts.setFocusPolicy(QtCore.Qt.NoFocus)
        self.hosts.setFixedHeight(118)
        self.hosts.header().setStretchLastSection(False)
        self.hosts.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.hosts.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        right.addWidget(self.hosts)

        right.addSpacing(4)
        right.addWidget(QtWidgets.QLabel("日志", objectName="Section"))
        self.log = QtWidgets.QPlainTextEdit(objectName="Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        right.addWidget(self.log, 1)

        self.setStyleSheet(
            """
            QWidget { font-family: "Noto Sans CJK SC", "Microsoft YaHei UI"; font-size: 10pt; color: #1f2329; }
            QWidget#Root { background: #f5f6f7; }
            QLabel#Status { font-weight: 600; }
            QLabel#Muted { color: #646a73; }
            QLabel#Section { color: #646a73; font-size: 9pt; }
            QFrame#Panel, QTreeWidget#Panel { background: #ffffff; border: 1px solid #dee0e3; border-radius: 6px; }
            QTreeWidget#Panel::item { padding: 3px 4px; }
            QLineEdit, QComboBox, QDoubleSpinBox { background: #ffffff; border: 1px solid #d0d3d6; border-radius: 4px; padding: 3px 6px; min-height: 22px; }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border-color: #8f959e; }
            QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled { color: #b4b9c1; background: #f5f6f7; }
            QComboBox::drop-down { border: none; width: 18px; }
            QPushButton, QToolButton { background: #ffffff; border: 1px solid #d0d3d6; border-radius: 4px; padding: 4px 12px; min-height: 20px; }
            QToolButton { padding: 3px 8px; min-height: 18px; }
            QPushButton:hover, QToolButton:hover { background: #f2f3f5; }
            QPushButton:pressed, QToolButton:pressed { background: #e8e9eb; }
            QPushButton#Primary { background: #1f2329; color: #ffffff; border-color: #1f2329; font-weight: 600; padding: 4px 16px; }
            QPushButton#Primary:hover { background: #373c43; }
            QPlainTextEdit#Log { background: #ffffff; border: 1px solid #dee0e3; border-radius: 6px; padding: 4px;
                                 font-family: "JetBrains Mono", "Noto Sans Mono", monospace; font-size: 9pt; color: #373c43; }
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
        self._sync_interval_enabled()

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
        section["max_seconds"] = section.get("max_seconds", "300")
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
        self.interval_spin.setValue(0.8 if backend == "qwen3" and qwen_backend != "vllm" else 0.5)
        self._sync_interval_enabled()
        self._refresh_models()

    def _sync_interval_enabled(self) -> None:
        streaming = self._backend_kind() == ("qwen3", "vllm")
        self.interval_spin.setEnabled(not streaming)
        self.interval_spin.setToolTip("流式后端自动约 0.2s 刷新一次" if streaming else "两次预览解码的间隔")

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
        self._set_running(False, "正在加载模型…", "#d29922")

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
        try:  # 同步落盘，便于事后排查（窗口日志关掉就没了）
            log_dir = Path(os.path.expanduser(self.data_edit.text().strip() or DEFAULT_DATA_DIR)) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "listen.log", "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass
        if "voice listener ready" in text:
            self._set_running(True)
        if "another voice-tunnel listener" in text:
            self._append("已有监听器在运行，请先停止旧的监听器。")
            self._set_running(False)

    def _process_finished(self, code: int, status: QtCore.QProcess.ExitStatus) -> None:
        self._append(f"监听进程已退出（code={code}）")
        self._set_running(False)

    def _process_error(self, error: QtCore.QProcess.ProcessError) -> None:
        self._append(f"监听进程错误：{error}")
        self._set_running(False)

    def _running_or_loading(self) -> bool:
        return self.proc is not None and self.proc.state() != QtCore.QProcess.NotRunning

    def _set_running(self, running: bool, text: str | None = None, color: str | None = None) -> None:
        if running:
            label, color = f"监听中 · 按住 {pretty_key(self.ptt_key)} 说话", "#2ea043"
        else:
            label, color = text or "已停止", color or "#b4b9c1"
        self.status_label.setText(label)
        self.status_dot.setStyleSheet(f"background:{color}; border-radius:4px;")
        busy = running or self._running_or_loading()
        self.run_btn.setText("停止监听" if busy else "开始监听")
        self.tray_start.setEnabled(not busy)
        self.tray_stop.setEnabled(busy)
        self.tray.setIcon(make_icon("#2ea043" if running else "#8f959e"))
        self.tray.setToolTip("Voice Tunnel · " + ("监听中" if running else label))

    # ------------------------------------------------------------------ status

    def _start_status_worker(self) -> None:
        self.worker = StatusWorker(CONFIG)
        self.worker.updated.connect(self._on_status)
        self.worker.start()

    def _on_status(self, data: dict[str, Any]) -> None:
        if data.get("error"):
            self.target_label.setText("服务端未连接")
            self.hosts.clear()
            return
        active = data.get("active_target")
        self.target_label.setText(f"当前目标：{active}" if active else "当前目标：无（鼠标所在屏幕没有 Agent）")
        agents = sorted(data.get("agents") or [], key=lambda a: a.get("host", ""))
        self.hosts.clear()
        for agent in agents:
            host = agent.get("host", "")
            is_target = host == active
            item = QtWidgets.QTreeWidgetItem([host, "当前目标" if is_target else "在线"])
            item.setForeground(1, QtGui.QColor("#2ea043" if is_target else "#8f959e"))
            if is_target:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.hosts.addTopLevelItem(item)
        current = self.target_combo.currentText()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem("自动（当前屏幕）")
        for agent in agents:
            self.target_combo.addItem(agent.get("host", ""))
        index = self.target_combo.findText(current)
        self.target_combo.setCurrentIndex(max(0, index))
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
