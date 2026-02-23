# gui.py
import sys
import os
import json
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                               QHBoxLayout, QCheckBox, QLabel, QLineEdit,
                               QTableWidget, QTableWidgetItem, QPushButton,
                               QHeaderView, QComboBox, QFileDialog, QProgressBar,
                               QMessageBox, QInputDialog)
from PySide6.QtCore import Qt, QThread, Signal

from sync_core import (sync, get_branches, SyncError, download_zipball,
                       extract_zip_to_temp, calculate_changes, local_mirror)

# Fixed repository
FIXED_REPO = "IGF-Ingenieure-GmbH/Revit"

# Config file stored next to the EXE (frozen) or next to the script (dev)
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")


# ──────────────────────────────────────────────
# Worker Threads
# ──────────────────────────────────────────────

class FetchBranchesThread(QThread):
    finished = Signal(int, object, str)  # row, branches (list), preset_branch
    error = Signal(int, str)            # row, error_msg

    def __init__(self, row, token, repo, preset_branch):
        super().__init__()
        self.row = row
        self.token = token
        self.repo = repo
        self.preset_branch = preset_branch

    def run(self):
        try:
            branches = get_branches(self.repo, self.token)
            self.finished.emit(self.row, branches, self.preset_branch)
        except Exception as e:
            print(f"[FetchBranches ERROR] repo={self.repo}: {e}")
            self.error.emit(self.row, str(e))


class CheckUpdatesThread(QThread):
    progress = Signal(int, str)     # row, status_msg
    finished = Signal(int, int)     # row, changes_count
    error = Signal(int, str)        # row, error_msg

    def __init__(self, row, repo, branch, token, local_dirs):
        super().__init__()
        self.row = row
        self.repo = repo
        self.branch = branch
        self.token = token
        self.local_dirs = local_dirs  # list of dirs to check

    def run(self):
        try:
            self.progress.emit(self.row, "ZIP-Archiv wird heruntergeladen...")
            zip_bytes = download_zipball(self.repo, self.branch, self.token, None)

            self.progress.emit(self.row, "Wird entpackt...")
            source_root = extract_zip_to_temp(zip_bytes, None)

            try:
                import tempfile, shutil
                total_changes = 0
                for local_dir in self.local_dirs:
                    if local_dir and os.path.isdir(local_dir):
                        changes, _ = calculate_changes(source_root, local_dir)
                        total_changes += changes
                    elif local_dir:
                        # Directory doesn't exist = everything is new
                        from sync_core import collect_files
                        total_changes += len(collect_files(source_root))
                self.finished.emit(self.row, total_changes)
            finally:
                # Clean up the temp root properly (walk up to temp base)
                import tempfile, shutil
                tmp_root = source_root
                tmp_base = tempfile.gettempdir()
                while os.path.dirname(tmp_root) != tmp_base and tmp_root != tmp_base:
                    tmp_root = os.path.dirname(tmp_root)
                shutil.rmtree(tmp_root, ignore_errors=True)
        except Exception as e:
            self.error.emit(self.row, str(e))


class SyncTaskThread(QThread):
    progress = Signal(int, int, int)    # row, col, value (0-100)
    status = Signal(int, int, str)      # row, col, msg
    finished = Signal(int, int)         # row, col
    error = Signal(int, int, str)       # row, col, error_msg

    def __init__(self, row, col, repo, branch, token, local_dir):
        super().__init__()
        self.row = row
        self.col = col  # 3 for Backup, 4 for Publish
        self.repo = repo
        self.branch = branch
        self.token = token
        self.local_dir = local_dir

    def run(self):
        try:
            if not self.local_dir:
                self.finished.emit(self.row, self.col)
                return
            if not os.path.exists(self.local_dir):
                os.makedirs(self.local_dir, exist_ok=True)

            def d_cb(mb, done):
                pass

            def s_cb(state, current, total, msg):
                if total > 0:
                    pct = int((current / total) * 100)
                    self.progress.emit(self.row, self.col, pct)
                self.status.emit(self.row, self.col, msg)

            sync(self.repo, self.branch, self.local_dir, self.token, None, d_cb, s_cb)
            self.finished.emit(self.row, self.col)
        except Exception as e:
            self.error.emit(self.row, self.col, str(e))


class BackupTaskThread(QThread):
    """Thread to mirror Publish folder → Backup folder (local-to-local)."""
    progress = Signal(int, int, int)    # row, col, value (0-100)
    status = Signal(int, int, str)      # row, col, msg
    finished = Signal(int, int)         # row, col
    error = Signal(int, int, str)       # row, col, error_msg

    def __init__(self, row, col, source_dir, target_dir):
        super().__init__()
        self.row = row
        self.col = col  # 3 for Backup column
        self.source_dir = source_dir
        self.target_dir = target_dir

    def run(self):
        try:
            if not self.source_dir or not os.path.isdir(self.source_dir):
                self.status.emit(self.row, self.col, "Quellordner nicht vorhanden")
                self.finished.emit(self.row, self.col)
                return
            os.makedirs(self.target_dir, exist_ok=True)

            def cb(state, current, total, msg):
                if total > 0:
                    pct = int((current / total) * 100)
                    self.progress.emit(self.row, self.col, pct)
                self.status.emit(self.row, self.col, msg)

            local_mirror(self.source_dir, self.target_dir, cb)
            self.finished.emit(self.row, self.col)
        except Exception as e:
            self.error.emit(self.row, self.col, str(e))


# ──────────────────────────────────────────────
# Custom Cell Widget for folder selection + progress
# ──────────────────────────────────────────────

class FolderCellWidget(QWidget):
    def __init__(self, path=""):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self.btn = QPushButton("...")
        self.btn.setToolTip("Ordner auswählen")
        self.btn.setFixedWidth(30)

        # Store the actual folder path separately from display text
        self._folder_path = path

        self.lbl = QLabel(path)
        self.lbl.setWordWrap(True)
        self.lbl.setAlignment(Qt.AlignCenter)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.progress.setFixedHeight(10)

        top_h = QHBoxLayout()
        top_h.addWidget(self.btn)
        top_h.addWidget(self.lbl, 1)
        top_h.setContentsMargins(0, 0, 0, 0)

        layout.addLayout(top_h)
        layout.addWidget(self.progress)

    @property
    def folder_path(self) -> str:
        return self._folder_path

    @folder_path.setter
    def folder_path(self, value: str):
        self._folder_path = value
        self.lbl.setText(value)

    def set_status(self, text: str):
        """Update label without changing the stored folder_path."""
        self.lbl.setText(text)

    def restore_label(self):
        """Restore label to show the folder path."""
        self.lbl.setText(self._folder_path)


# ──────────────────────────────────────────────
# Main Window
# ──────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Github Sync")
        self.resize(1000, 600)
        self.setStyleSheet("QMainWindow { background-color: #87CEEB; }")

        self._active_threads: list[QThread] = []  # Track running threads

        self.config = self._load_config()
        self._init_ui()
        self._populate_from_config()

    # ── Config persistence ──

    def _load_config(self) -> dict:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_config(self):
        config = {
            "token": self.token_input.text(),
            "backup_enabled": self.cb_backup.isChecked(),
            "publish_enabled": self.cb_publish.isChecked(),
            "rows": []
        }
        for i in range(self.table.rowCount()):
            checkbox = self.table.cellWidget(i, 0)
            cb = checkbox.findChild(QCheckBox) if checkbox else None
            is_checked = cb.isChecked() if cb else False

            combo = self.table.cellWidget(i, 1)
            branch = combo.currentText() if combo else ""

            backup_w = self.table.cellWidget(i, 3)
            backup_dir = backup_w.folder_path if backup_w else ""

            pub_w = self.table.cellWidget(i, 4)
            pub_dir = pub_w.folder_path if pub_w else ""

            config["rows"].append({
                "checked": is_checked,
                "branch": branch,
                "backup": backup_dir,
                "publish": pub_dir
            })
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print("Failed to save config:", e)

    def closeEvent(self, event):
        self._save_config()
        super().closeEvent(event)

    # ── UI setup ──

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Top layout: Checkboxes
        top_layout = QHBoxLayout()
        self.cb_backup = QCheckBox("Sicherung aktivieren")
        self.cb_backup.setChecked(True)
        self.cb_publish = QCheckBox("Veröffentlichung aktivieren")
        self.cb_publish.setChecked(True)
        top_layout.addWidget(self.cb_backup)
        top_layout.addWidget(self.cb_publish)
        main_layout.addLayout(top_layout)

        # Token Layout
        token_layout = QHBoxLayout()
        token_lbl = QLabel("Github-token:")
        token_lbl.setFixedWidth(100)
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.editingFinished.connect(self._on_token_changed)
        token_layout.addWidget(token_lbl)
        token_layout.addWidget(self.token_input)
        main_layout.addLayout(token_layout)

        # DataGrid / TableWidget
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "", "Branch", "Änderungen",
            "Sicherungsordner", "Veröffentlichungsordner"
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 150)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setStyleSheet("QTableWidget { background-color: #ADD8E6; }")
        main_layout.addWidget(self.table)

        # Bottom Buttons Layout
        bottom_layout = QHBoxLayout()
        self.btn_add = QPushButton("Branch hinzufügen")
        self.btn_del = QPushButton("Branch löschen")
        self.btn_check = QPushButton("Update prüfen")
        self.btn_start = QPushButton("Update starten")
        self.btn_cancel = QPushButton("Abbrechen")

        self.btn_add.clicked.connect(self._add_branch_dialog)
        self.btn_del.clicked.connect(self._del_branch)
        self.btn_check.clicked.connect(self._check_updates)
        self.btn_start.clicked.connect(self._start_updates)
        self.btn_cancel.clicked.connect(self.close)

        bottom_layout.addWidget(self.btn_add)
        bottom_layout.addWidget(self.btn_del)
        bottom_layout.addWidget(self.btn_check)
        bottom_layout.addWidget(self.btn_start)
        bottom_layout.addWidget(self.btn_cancel)
        main_layout.addLayout(bottom_layout)

    def _populate_from_config(self):
        self.token_input.setText(self.config.get("token", ""))
        self.cb_backup.setChecked(self.config.get("backup_enabled", True))
        self.cb_publish.setChecked(self.config.get("publish_enabled", True))

        for row_data in self.config.get("rows", []):
            self._add_row(
                row_data.get("branch", ""),
                row_data.get("backup", ""),
                row_data.get("publish", ""),
                row_data.get("checked", True)
            )

    # ── Row management ──

    def _add_row(self, branch, backup, publish, checked=True):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 50)

        # Column 0: Checkbox
        cb_widget = QWidget()
        cb_layout = QHBoxLayout(cb_widget)
        cb_layout.setContentsMargins(0, 0, 0, 0)
        cb_layout.setAlignment(Qt.AlignCenter)
        cb = QCheckBox()
        cb.setChecked(checked)
        cb_layout.addWidget(cb)
        self.table.setCellWidget(row, 0, cb_widget)

        # Column 1: Branch Combobox
        combo = QComboBox()
        if branch:
            combo.addItem(branch)
        self.table.setCellWidget(row, 1, combo)

        # Column 2: Changes count
        item_changes = QTableWidgetItem("?")
        item_changes.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        item_changes.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, 2, item_changes)

        # Column 3: Backup Folder
        w_backup = FolderCellWidget(backup)
        w_backup.btn.clicked.connect(lambda checked=False, w=w_backup: self._select_folder(w))
        self.table.setCellWidget(row, 3, w_backup)

        # Column 4: Publish Folder
        w_publish = FolderCellWidget(publish)
        w_publish.btn.clicked.connect(lambda checked=False, w=w_publish: self._select_folder(w))
        self.table.setCellWidget(row, 4, w_publish)

        # Fetch branches dynamically (only if token is available)
        token = self.token_input.text().strip()
        if token:
            self._fetch_branches_for_row(row, branch)
        else:
            combo = self.table.cellWidget(row, 1)
            if combo:
                combo.clear()
                combo.addItem("Bitte Token eingeben")
                combo.setEnabled(False)

    def _select_folder(self, folder_widget: FolderCellWidget):
        dir_path = QFileDialog.getExistingDirectory(self, "Ordner auswählen")
        if dir_path:
            folder_widget.folder_path = dir_path

    def _add_branch_dialog(self):
        self._add_row("", "", "")

    def _del_branch(self):
        curr_row = self.table.currentRow()
        if curr_row >= 0:
            self.table.removeRow(curr_row)

    # ── Token change handler ──

    def _on_token_changed(self):
        """Re-fetch branches for all rows when token is entered/changed."""
        token = self.token_input.text().strip()
        if not token:
            return
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 1)
            if combo:
                current = combo.currentText()
                # Re-fetch if loading failed or placeholder is shown
                if current in ("Laden fehlgeschlagen", "Bitte Token eingeben", "Wird geladen...", ""):
                    self._fetch_branches_for_row(row, "")

    # ── Branch fetching ──

    def _fetch_branches_for_row(self, row, preset_branch=""):
        token = self.token_input.text().strip()
        combo = self.table.cellWidget(row, 1)
        if combo:
            combo.clear()
            combo.addItem("Wird geladen...")
            combo.setEnabled(False)

        thread = FetchBranchesThread(row, token, FIXED_REPO, preset_branch)
        thread.finished.connect(self._on_branches_fetched)
        thread.error.connect(self._on_branches_error)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._remove_thread(thread))
        thread.error.connect(lambda: self._remove_thread(thread))
        thread.start()

    def _on_branches_fetched(self, row, branches, preset_branch):
        if row >= self.table.rowCount():
            return
        combo = self.table.cellWidget(row, 1)
        if combo:
            combo.clear()
            combo.addItems(branches)
            combo.setEnabled(True)
            if preset_branch and preset_branch in branches:
                combo.setCurrentText(preset_branch)

    def _on_branches_error(self, row, error):
        if row >= self.table.rowCount():
            return
        print(f"[Branch ERROR] row={row}: {error}")
        combo = self.table.cellWidget(row, 1)
        if combo:
            combo.clear()
            combo.addItem("Laden fehlgeschlagen")
            combo.setToolTip(error)

    # ── Check Updates ──

    def _check_updates(self):
        self.btn_check.setEnabled(False)
        self.btn_start.setEnabled(False)

        token = self.token_input.text().strip()
        pending = 0

        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, 0).findChild(QCheckBox)
            if not cb or not cb.isChecked():
                continue

            combo = self.table.cellWidget(row, 1)
            branch = combo.currentText() if combo else ""
            if not branch or branch in ("Wird geladen...", "Laden fehlgeschlagen"):
                continue

            bk_dir = self.table.cellWidget(row, 3).folder_path if self.cb_backup.isChecked() else None
            pu_dir = self.table.cellWidget(row, 4).folder_path if self.cb_publish.isChecked() else None

            # Check updates only applies to publish folder vs remote
            dirs = [pu_dir] if pu_dir else []
            if not dirs:
                continue

            self.table.item(row, 2).setText("Wird geprüft...")
            pending += 1

            th = CheckUpdatesThread(row, FIXED_REPO, branch, token, dirs)
            th.progress.connect(self._on_check_progress)
            th.finished.connect(self._on_check_finished)
            th.error.connect(self._on_check_error)
            self._active_threads.append(th)
            th.finished.connect(lambda r, c, t=th: self._remove_thread_and_maybe_reenable(t))
            th.error.connect(lambda r, e, t=th: self._remove_thread_and_maybe_reenable(t))
            th.start()

        if pending == 0:
            self.btn_check.setEnabled(True)
            self.btn_start.setEnabled(True)

    def _on_check_progress(self, row, msg):
        if row < self.table.rowCount():
            self.table.item(row, 2).setText(msg)

    def _on_check_finished(self, row, changes):
        if row < self.table.rowCount():
            self.table.item(row, 2).setText(f"{changes} Dateien zu aktualisieren" if changes > 0 else "Aktuell ✅")

    def _on_check_error(self, row, err):
        if row < self.table.rowCount():
            self.table.item(row, 2).setText("Fehlgeschlagen ❌")
            self.table.item(row, 2).setToolTip(err)

    # ── Start Updates ──

    def _start_updates(self):
        self.btn_check.setEnabled(False)
        self.btn_start.setEnabled(False)
        token = self.token_input.text().strip()
        pending = 0

        for row in range(self.table.rowCount()):
            cb = self.table.cellWidget(row, 0).findChild(QCheckBox)
            if not cb or not cb.isChecked():
                continue

            combo = self.table.cellWidget(row, 1)
            branch = combo.currentText() if combo else ""
            if not branch or branch in ("Wird geladen...", "Laden fehlgeschlagen", "Bitte Token eingeben"):
                continue

            bk_dir = self.table.cellWidget(row, 3).folder_path
            pu_dir = self.table.cellWidget(row, 4).folder_path

            # Step 1: If Sicherung enabled, backup Publish → Sicherung
            if self.cb_backup.isChecked() and bk_dir and pu_dir:
                self._run_backup(row, 3, pu_dir, bk_dir)
                pending += 1

            # Step 2: If Veröffentlichung enabled, sync Remote → Publish
            if self.cb_publish.isChecked() and pu_dir:
                self._run_sync(row, 4, FIXED_REPO, branch, token, pu_dir)
                pending += 1

        if pending == 0:
            self.btn_check.setEnabled(True)
            self.btn_start.setEnabled(True)

    def _run_backup(self, row, col, source_dir, target_dir):
        w = self.table.cellWidget(row, col)
        if not w:
            return
        w.progress.setVisible(True)
        w.progress.setValue(0)
        w.set_status("Sicherung wird erstellt...")

        th = BackupTaskThread(row, col, source_dir, target_dir)
        th.progress.connect(self._on_sync_progress)
        th.status.connect(self._on_sync_status)
        th.finished.connect(self._on_sync_finished)
        th.error.connect(self._on_sync_error)
        self._active_threads.append(th)
        th.finished.connect(lambda r, c, t=th: self._remove_thread_and_maybe_reenable(t))
        th.error.connect(lambda r, c, e, t=th: self._remove_thread_and_maybe_reenable(t))
        th.start()

    def _run_sync(self, row, col, repo, branch, token, local_dir):
        w = self.table.cellWidget(row, col)
        if not w:
            return
        w.progress.setVisible(True)
        w.progress.setValue(0)
        w.set_status("Wird vorbereitet...")

        th = SyncTaskThread(row, col, repo, branch, token, local_dir)
        th.progress.connect(self._on_sync_progress)
        th.status.connect(self._on_sync_status)
        th.finished.connect(self._on_sync_finished)
        th.error.connect(self._on_sync_error)
        self._active_threads.append(th)
        th.finished.connect(lambda r, c, t=th: self._remove_thread_and_maybe_reenable(t))
        th.error.connect(lambda r, c, e, t=th: self._remove_thread_and_maybe_reenable(t))
        th.start()

    def _on_sync_progress(self, row, col, val):
        w = self.table.cellWidget(row, col)
        if w:
            w.progress.setValue(val)

    def _on_sync_status(self, row, col, msg):
        w = self.table.cellWidget(row, col)
        if w:
            w.set_status(msg)

    def _on_sync_finished(self, row, col):
        w = self.table.cellWidget(row, col)
        if w:
            w.progress.setValue(100)
            w.set_status("Synchronisierung abgeschlossen ✅")

    def _on_sync_error(self, row, col, err):
        w = self.table.cellWidget(row, col)
        if w:
            w.progress.setValue(0)
            w.set_status(f"Fehler: {err}")
            QMessageBox.warning(self, "Synchronisierungsfehler", f"Zeile {row + 1}: Ein Fehler ist aufgetreten:\n{err}")

    # ── Thread lifecycle helpers ──

    def _remove_thread(self, thread):
        if thread in self._active_threads:
            self._active_threads.remove(thread)

    def _remove_thread_and_maybe_reenable(self, thread):
        self._remove_thread(thread)
        # Re-enable buttons when no more active work threads remain
        has_work = any(
            isinstance(t, (CheckUpdatesThread, SyncTaskThread))
            for t in self._active_threads
        )
        if not has_work:
            self.btn_check.setEnabled(True)
            self.btn_start.setEnabled(True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
