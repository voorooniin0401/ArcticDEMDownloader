import json
import os
import time
import traceback
import urllib.request
import tarfile
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

from qgis.PyQt.QtCore import pyqtSignal, QObject
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QFileDialog, QSpinBox, QMessageBox, QCheckBox, QProgressBar, QTableWidget,
    QTableWidgetItem, QHeaderView, QTextEdit, QGroupBox
)
from qgis.core import (
    QgsApplication, QgsMessageLog, QgsTask, Qgis, QgsVectorLayer, QgsProject,
    QgsRasterLayer
)

try:
    from osgeo import gdal
except Exception:
    gdal = None


PLUGIN_NAME = "ArcticDEMDownloader"

INDEX_FILES = {
    "2 м": "ArcticDEM_Mosaic_Index_v4_1_2m.shp",
    "10 м": "ArcticDEM_Mosaic_Index_v4_1_10m.shp",
    "32 м": "ArcticDEM_Mosaic_Index_v4_1_32m.shp",
}


def plugin_root():
    return Path(__file__).resolve().parent


def index_search_dirs():
    root = plugin_root()
    return [root / "indexes", root]


def find_index_file(filename):
    for folder in index_search_dirs():
        path = folder / filename
        if path.exists():
            return path
    return None

def icon_path(icon_name):
    return str(plugin_root() / "icons" / icon_name)


def human_size(num_bytes):
    if num_bytes is None or num_bytes < 0:
        return "—"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}" if unit != "Б" else f"{int(size)} {unit}"
        size /= 1024


def human_speed(bytes_per_second):
    if bytes_per_second is None or bytes_per_second <= 0:
        return "—"
    return f"{human_size(bytes_per_second)}/с"


def filename_from_url(url):
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    return name if name else "arcticdem_tile.tif"


def is_archive(path):
    name = path.name.lower()
    return name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar") or name.endswith(".zip")


def is_dem_tif_name(name):
    low = name.lower()
    if not (low.endswith(".tif") or low.endswith(".tiff")):
        return False
    base = Path(low).name
    return (
        "_dem" in base
        or base.endswith("dem.tif")
        or base.endswith("dem.tiff")
    )


class SharedDownloadState:
    def __init__(self):
        self.paused_files = {}
        self.stopped = False

    def pause_file(self, filename):
        self.paused_files[filename] = True

    def resume_file(self, filename):
        self.paused_files[filename] = False

    def is_paused(self, filename):
        return bool(self.paused_files.get(filename, False))

    def stop(self):
        self.stopped = True


class ProgressSignals(QObject):
    file_progress = pyqtSignal(str, str, int, int, float, int)
    total_progress = pyqtSignal(int, int, int, int, int, float)
    message = pyqtSignal(str)
    finished = pyqtSignal(bool, int, int, int, int, list, str)


class ArcticDEMDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.download_action = None
        self.load_indexes_action = None
        self.vrt_action = None
        self.toolbar = None
        self.progress_dialog = None
        self.current_task = None
        self.shared_state = None

    def initGui(self):
        self.toolbar = self.iface.addToolBar(PLUGIN_NAME)
        self.toolbar.setObjectName(PLUGIN_NAME)

        self.load_indexes_action = QAction(
            QIcon(icon_path("index.svg")),
            "Подгрузить индексы ArcticDEM",
            self.iface.mainWindow()
        )
        self.load_indexes_action.setToolTip("Подгрузить индексы ArcticDEM")
        self.load_indexes_action.setStatusTip("Подгрузить shapefile-индексы ArcticDEM 2 м, 10 м или 32 м")
        self.load_indexes_action.triggered.connect(self.load_indexes_dialog)

        self.download_action = QAction(
            QIcon(icon_path("download.svg")),
            "Скачать выбранные тайлы",
            self.iface.mainWindow()
        )
        self.download_action.setToolTip("Скачать выбранные тайлы")
        self.download_action.setStatusTip("Скачать выделенные тайлы ArcticDEM")
        self.download_action.triggered.connect(self.run_download)

        self.vrt_action = QAction(
            QIcon(icon_path("vrt.svg")),
            "Создать VRT из папки DEM",
            self.iface.mainWindow()
        )
        self.vrt_action.setToolTip("Создать VRT из папки DEM")
        self.vrt_action.setStatusTip("Создать ArcticDEM.vrt по DEM")
        self.vrt_action.triggered.connect(self.create_vrt_from_folder)

        self.iface.addPluginToMenu(PLUGIN_NAME, self.load_indexes_action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.download_action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.vrt_action)

        self.toolbar.addAction(self.load_indexes_action)
        self.toolbar.addAction(self.download_action)
        self.toolbar.addAction(self.vrt_action)

    def unload(self):
        for action in [self.load_indexes_action, self.download_action, self.vrt_action]:
            if action:
                self.iface.removePluginMenu(PLUGIN_NAME, action)

        if self.toolbar:
            del self.toolbar

    def load_indexes_dialog(self):
        dialog = LoadIndexesDialog()
        if dialog.exec_() != QDialog.Accepted:
            return

        selected = dialog.selected_resolutions()
        if not selected:
            QMessageBox.warning(None, PLUGIN_NAME, "Не выбран ни один индекс ArcticDEM.")
            return

        loaded, missing, invalid = [], [], []

        for resolution in selected:
            filename = INDEX_FILES[resolution]
            shp_path = find_index_file(filename)

            if shp_path is None:
                missing.append(filename)
                continue

            layer_name = f"ArcticDEM Mosaic Index v4.1 {resolution}"
            layer = QgsVectorLayer(str(shp_path), layer_name, "ogr")

            if not layer.isValid():
                invalid.append(filename)
                continue

            QgsProject.instance().addMapLayer(layer)
            loaded.append(layer_name)

        msg_parts = []
        if loaded:
            msg_parts.append("Загружены слои:\n" + "\n".join(f"• {x}" for x in loaded))
        if missing:
            msg_parts.append("Не найдены файлы:\n" + "\n".join(f"• {x}" for x in missing))
        if invalid:
            msg_parts.append("Не удалось открыть:\n" + "\n".join(f"• {x}" for x in invalid))

        msg = "\n\n".join(msg_parts) if msg_parts else "Ничего не загружено."
        if missing or invalid:
            QMessageBox.warning(None, PLUGIN_NAME, msg)
        else:
            QMessageBox.information(None, PLUGIN_NAME, msg)

    def create_vrt_from_folder(self):
        folder = QFileDialog.getExistingDirectory(self.iface.mainWindow(), "Выбрать папку с DEM GeoTIFF")
        if not folder:
            return

        folder_path = Path(folder)
        tifs = find_dem_tifs(folder_path)
        if not tifs:
            QMessageBox.warning(None, PLUGIN_NAME, "В выбранной папке не найдено DEM-файлов *_dem*.tif.")
            return

        vrt_path = folder_path / "ArcticDEM.vrt"
        ok, msg = build_vrt(vrt_path, tifs)
        if not ok:
            QMessageBox.critical(None, PLUGIN_NAME, msg)
            return

        add_raster_to_project(vrt_path, "ArcticDEM VRT")
        QMessageBox.information(None, PLUGIN_NAME, f"VRT создан и добавлен в проект:\n{vrt_path}")

    def run_download(self):
        layer = self.iface.activeLayer()

        if layer is None:
            QMessageBox.warning(None, PLUGIN_NAME, "Активный слой не выбран.")
            return

        if layer.selectedFeatureCount() == 0:
            QMessageBox.warning(
                None, PLUGIN_NAME,
                "В активном слое нет выделенных объектов. Выдели один или несколько листов ArcticDEM."
            )
            return

        field_names = [field.name() for field in layer.fields()]
        if "fileurl" not in field_names:
            QMessageBox.warning(
                None, PLUGIN_NAME,
                "В активном слое нет поля 'fileurl'. Проверь, что выбран именно индексный слой ArcticDEM."
            )
            return

        urls = []
        for feature in layer.selectedFeatures():
            value = feature["fileurl"]
            if value:
                url = str(value).strip()
                if url.startswith("http"):
                    urls.append(url)

        urls = sorted(set(urls))

        if not urls:
            QMessageBox.warning(None, PLUGIN_NAME, "В выделенных объектах нет корректных ссылок в поле 'fileurl'.")
            return

        dialog = DownloadDialog(len(urls))
        if dialog.exec_() != QDialog.Accepted:
            return

        output_dir = dialog.output_dir()
        file_workers = dialog.file_workers()
        segment_workers = dialog.segment_workers()
        overwrite = dialog.overwrite()
        auto_extract = dialog.auto_extract()
        dem_only = dialog.dem_only()
        delete_archives = dialog.delete_archives()
        auto_vrt = dialog.auto_vrt()
        add_vrt = dialog.add_vrt()

        if not output_dir:
            QMessageBox.warning(None, PLUGIN_NAME, "Не выбрана папка для сохранения.")
            return

        file_names = [filename_from_url(u) for u in urls]

        self.shared_state = SharedDownloadState()
        signals = ProgressSignals()

        self.progress_dialog = ProgressDialog(file_names, self.shared_state, self.iface.mainWindow())
        self.progress_dialog.cancel_requested.connect(self.cancel_current_task)

        signals.file_progress.connect(self.progress_dialog.update_file_progress)
        signals.total_progress.connect(self.progress_dialog.update_total_progress)
        signals.message.connect(self.progress_dialog.add_message)
        signals.finished.connect(self.on_task_finished)
        signals.finished.connect(self.progress_dialog.finish)

        self.current_task = DownloadTask(
            urls=urls, output_dir=output_dir, file_workers=file_workers,
            segment_workers=segment_workers, overwrite=overwrite,
            signals=signals, shared_state=self.shared_state,
            auto_extract=auto_extract, dem_only=dem_only,
            delete_archives=delete_archives, auto_vrt=auto_vrt,
            add_vrt_to_project=add_vrt
        )
        QgsApplication.taskManager().addTask(self.current_task)
        self.progress_dialog.show()

        self.iface.messageBar().pushMessage(
            PLUGIN_NAME, f"Запущено скачивание: {len(urls)} файл(ов).",
            level=Qgis.Info, duration=5
        )

    def on_task_finished(self, success, downloaded, skipped, errors, completed, incomplete_files, vrt_path):
        if vrt_path:
            layer = QgsRasterLayer(vrt_path, "ArcticDEM VRT")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                self.iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    "VRT создан и добавлен в проект QGIS.",
                    level=Qgis.Success,
                    duration=6
                )
            else:
                QgsMessageLog.logMessage(f"VRT создан, но не добавился в QGIS: {vrt_path}", PLUGIN_NAME, Qgis.Warning)

        if incomplete_files:
            QMessageBox.warning(
                None,
                PLUGIN_NAME,
                "Скачивание завершено не полностью.\n\nНе полностью скачаны:\n" +
                "\n".join(incomplete_files[:30])
            )

    def cancel_current_task(self):
        if self.shared_state:
            self.shared_state.stop()
        if self.current_task:
            self.current_task.cancel()


class LoadIndexesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{PLUGIN_NAME} — индексы")
        self.setMinimumWidth(560)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Выбери индексы ArcticDEM, которые нужно добавить в проект QGIS."))

        folder_text = "Плагин ищет shapefile в папках:\n"
        folder_text += f"1) {plugin_root() / 'indexes'}\n"
        folder_text += f"2) {plugin_root()}"
        label = QLabel(folder_text)
        label.setWordWrap(True)
        layout.addWidget(label)

        self.checkboxes = {}
        group = QGroupBox("Доступные разрешения")
        group_layout = QVBoxLayout()

        for resolution, filename in INDEX_FILES.items():
            path = find_index_file(filename)
            status = "найден" if path else "не найден"
            box = QCheckBox(f"{resolution} — {filename} ({status})")
            box.setChecked(path is not None)
            box.setEnabled(path is not None)
            self.checkboxes[resolution] = box
            group_layout.addWidget(box)

        group.setLayout(group_layout)
        layout.addWidget(group)
        layout.addWidget(QLabel("Важно: рядом с .shp должны лежать .dbf, .shx и желательно .prj."))

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()
        self.load_btn = QPushButton("Подгрузить")
        self.cancel_btn = QPushButton("Отмена")
        self.load_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        buttons_row.addWidget(self.load_btn)
        buttons_row.addWidget(self.cancel_btn)
        layout.addLayout(buttons_row)
        self.setLayout(layout)

    def selected_resolutions(self):
        return [res for res, cb in self.checkboxes.items() if cb.isChecked() and cb.isEnabled()]


class DownloadDialog(QDialog):
    def __init__(self, url_count, parent=None):
        super().__init__(parent)
        self.setWindowTitle(PLUGIN_NAME)
        self.setMinimumWidth(680)

        layout = QVBoxLayout()
        layout.addWidget(QLabel(f"Найдено ссылок fileurl: {url_count}"))

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_btn = QPushButton("Выбрать папку")
        self.folder_btn.clicked.connect(self.choose_folder)
        folder_row.addWidget(QLabel("Папка сохранения:"))
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(self.folder_btn)
        layout.addLayout(folder_row)

        file_workers_row = QHBoxLayout()
        self.file_workers_spin = QSpinBox()
        self.file_workers_spin.setMinimum(1)
        self.file_workers_spin.setMaximum(8)
        self.file_workers_spin.setValue(2)
        file_workers_row.addWidget(QLabel("Одновременных файлов:"))
        file_workers_row.addWidget(self.file_workers_spin)
        file_workers_row.addStretch()
        layout.addLayout(file_workers_row)

        segment_workers_row = QHBoxLayout()
        self.segment_workers_spin = QSpinBox()
        self.segment_workers_spin.setMinimum(1)
        self.segment_workers_spin.setMaximum(16)
        self.segment_workers_spin.setValue(4)
        segment_workers_row.addWidget(QLabel("Потоков на один файл через Range:"))
        segment_workers_row.addWidget(self.segment_workers_spin)
        segment_workers_row.addStretch()
        layout.addLayout(segment_workers_row)

        layout.addWidget(QLabel("Рекомендация: 1–2 файла одновременно и 4–6 Range-потоков на файл."))

        self.overwrite_box = QCheckBox("Перекачивать уже существующие файлы")
        self.overwrite_box.setChecked(False)
        layout.addWidget(self.overwrite_box)

        post_group = QGroupBox("После скачивания")
        post_layout = QVBoxLayout()

        self.auto_extract_box = QCheckBox("Автоматически распаковать архивы")
        self.auto_extract_box.setChecked(True)
        post_layout.addWidget(self.auto_extract_box)

        self.dem_only_box = QCheckBox("Извлекать только DEM GeoTIFF (*_dem*.tif). Если выключено — извлекать все файлы архива")
        self.dem_only_box.setChecked(False)
        post_layout.addWidget(self.dem_only_box)

        self.delete_archives_box = QCheckBox("Удалить архивы после успешной распаковки")
        self.delete_archives_box.setChecked(False)
        post_layout.addWidget(self.delete_archives_box)

        self.auto_vrt_box = QCheckBox("Автоматически создать VRT после распаковки по DEM GeoTIFF")
        self.auto_vrt_box.setChecked(True)
        post_layout.addWidget(self.auto_vrt_box)

        self.add_vrt_box = QCheckBox("Добавить VRT в проект QGIS")
        self.add_vrt_box.setChecked(True)
        post_layout.addWidget(self.add_vrt_box)

        post_group.setLayout(post_layout)
        layout.addWidget(post_group)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()
        self.start_btn = QPushButton("Скачать")
        self.cancel_btn = QPushButton("Отмена")
        self.start_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        buttons_row.addWidget(self.start_btn)
        buttons_row.addWidget(self.cancel_btn)
        layout.addLayout(buttons_row)
        self.setLayout(layout)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбрать папку для ArcticDEM")
        if folder:
            self.folder_edit.setText(folder)

    def output_dir(self):
        return self.folder_edit.text().strip()

    def file_workers(self):
        return int(self.file_workers_spin.value())

    def segment_workers(self):
        return int(self.segment_workers_spin.value())

    def overwrite(self):
        return bool(self.overwrite_box.isChecked())

    def auto_extract(self):
        return bool(self.auto_extract_box.isChecked())

    def dem_only(self):
        return bool(self.dem_only_box.isChecked())

    def delete_archives(self):
        return bool(self.delete_archives_box.isChecked())

    def auto_vrt(self):
        return bool(self.auto_vrt_box.isChecked())

    def add_vrt(self):
        return bool(self.add_vrt_box.isChecked())


class ProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    COL_FILE = 0
    COL_STATUS = 1
    COL_SIZE = 2
    COL_SPEED = 3
    COL_PROGRESS = 4
    COL_ACTION = 5

    def __init__(self, file_names, shared_state, parent=None):
        super().__init__(parent)
        self.file_names = file_names
        self.shared_state = shared_state
        self.rows = {}

        self.setWindowTitle(f"{PLUGIN_NAME} — прогресс скачивания")
        self.setMinimumWidth(1120)
        self.setMinimumHeight(700)

        layout = QVBoxLayout()
        self.status_label = QLabel("Подготовка к скачиванию...")
        layout.addWidget(self.status_label)

        self.total_bar = QProgressBar()
        self.total_bar.setMinimum(0)
        self.total_bar.setMaximum(len(file_names))
        self.total_bar.setValue(0)
        self.total_bar.setFormat("Общий прогресс: %v из %m файлов (%p%)")
        layout.addWidget(self.total_bar)

        self.total_info_label = QLabel("Скачано всего: 0 Б | Общая скорость: — | Скачано: 0 | Пропущено: 0 | Ошибки: 0")
        layout.addWidget(self.total_info_label)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Файл", "Статус", "Скачано / размер", "Скорость", "Прогресс", "Пауза"])
        self.table.setRowCount(len(file_names))

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_FILE, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_SIZE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_SPEED, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_PROGRESS, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self.COL_ACTION, QHeaderView.ResizeToContents)

        for row, name in enumerate(file_names):
            self.rows[name] = row
            self.shared_state.resume_file(name)
            self.table.setItem(row, self.COL_FILE, QTableWidgetItem(name))
            self.table.setItem(row, self.COL_STATUS, QTableWidgetItem("Ожидает"))
            self.table.setItem(row, self.COL_SIZE, QTableWidgetItem("0 Б / —"))
            self.table.setItem(row, self.COL_SPEED, QTableWidgetItem("—"))

            bar = QProgressBar()
            bar.setMinimum(0)
            bar.setMaximum(100)
            bar.setValue(0)
            bar.setFormat("%p%")
            self.table.setCellWidget(row, self.COL_PROGRESS, bar)

            btn = QPushButton("Пауза")
            btn.clicked.connect(lambda checked=False, fn=name, b=btn: self.toggle_pause(fn, b))
            self.table.setCellWidget(row, self.COL_ACTION, btn)

        layout.addWidget(self.table)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(170)
        layout.addWidget(self.log)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()
        self.cancel_btn = QPushButton("Отменить всё")
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        self.close_btn.clicked.connect(self.close)
        buttons_row.addWidget(self.cancel_btn)
        buttons_row.addWidget(self.close_btn)
        layout.addLayout(buttons_row)
        self.setLayout(layout)

    def toggle_pause(self, filename, button):
        if self.shared_state.is_paused(filename):
            self.shared_state.resume_file(filename)
            button.setText("Пауза")
            row = self.rows.get(filename)
            if row is not None:
                self.table.item(row, self.COL_STATUS).setText("Возобновляется")
            self.add_message(f"▶ Возобновлен: {filename}")
        else:
            self.shared_state.pause_file(filename)
            button.setText("Продолжить")
            row = self.rows.get(filename)
            if row is not None:
                self.table.item(row, self.COL_STATUS).setText("Пауза")
            self.add_message(f"⏸ Пауза: {filename}")

    def update_file_progress(self, filename, status, downloaded, total, speed, percent):
        row = self.rows.get(filename)
        if row is None:
            return

        if not self.shared_state.is_paused(filename) or status in ("Пауза", "Готово", "Пропущен", "Ошибка", "Не полностью"):
            self.table.item(row, self.COL_STATUS).setText(status)

        self.table.item(row, self.COL_SIZE).setText(f"{human_size(downloaded)} / {human_size(total)}")
        self.table.item(row, self.COL_SPEED).setText(human_speed(speed))
        bar = self.table.cellWidget(row, self.COL_PROGRESS)
        if bar:
            bar.setValue(max(0, min(100, int(percent))))

    def update_total_progress(self, completed, downloaded_count, skipped_count, error_count, total_downloaded, total_speed):
        self.total_bar.setValue(completed)
        self.status_label.setText(f"Обработано файлов: {completed} из {len(self.file_names)}")
        self.total_info_label.setText(
            f"Скачано всего: {human_size(total_downloaded)} | "
            f"Общая скорость: {human_speed(total_speed)} | "
            f"Скачано: {downloaded_count} | Пропущено: {skipped_count} | Ошибки: {error_count}"
        )

    def add_message(self, text):
        self.log.append(text)

    def finish(self, success, downloaded, skipped, errors, completed, incomplete_files, vrt_path):
        if success and not incomplete_files:
            self.status_label.setText("Скачивание и обработка завершены.")
        elif incomplete_files:
            self.status_label.setText("Скачивание завершено не полностью.")
        else:
            self.status_label.setText("Скачивание остановлено или завершилось с ошибкой.")

        self.total_bar.setValue(completed)
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

        for row in range(self.table.rowCount()):
            btn = self.table.cellWidget(row, self.COL_ACTION)
            if btn:
                btn.setEnabled(False)

        self.log.append("")
        self.log.append("ИТОГ:")
        self.log.append(f"Скачано полностью: {downloaded}")
        self.log.append(f"Пропущено: {skipped}")
        self.log.append(f"Ошибки: {errors}")

        if vrt_path:
            self.log.append(f"VRT: {vrt_path}")

        if incomplete_files:
            self.log.append("")
            self.log.append("НЕ ПОЛНОСТЬЮ СКАЧАНЫ:")
            for name in incomplete_files:
                self.log.append(f"× {name}")


def safe_extract_tar_member(tar, member, out_dir, dem_only=True):
    if member.isdir():
        return None

    name = member.name.replace("\\", "/")
    filename = Path(name).name

    if dem_only and not is_dem_tif_name(filename):
        return None

    if not dem_only and filename == "":
        return None

    target = unique_path(out_dir / filename)
    src = tar.extractfile(member)
    if src is None:
        return None

    with open(target, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    return target


def safe_extract_zip_member(zf, info, out_dir, dem_only=True):
    if info.is_dir():
        return None

    filename = Path(info.filename.replace("\\", "/")).name

    if dem_only and not is_dem_tif_name(filename):
        return None

    target = unique_path(out_dir / filename)
    with zf.open(info, "r") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    return target


def unique_path(path):
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def extract_archives(output_dir, dem_only=True, delete_archives=False, emit=None):
    output_dir = Path(output_dir)
    extracted_dir = output_dir / "extracted_files"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    archives = [p for p in output_dir.iterdir() if p.is_file() and is_archive(p)]
    extracted = []
    errors = []

    if emit:
        emit(f"Архивов для распаковки: {len(archives)}")

    for archive in archives:
        try:
            if emit:
                emit(f"Распаковка: {archive.name}")

            lower = archive.name.lower()

            if lower.endswith(".zip"):
                with zipfile.ZipFile(archive, "r") as zf:
                    for info in zf.infolist():
                        result = safe_extract_zip_member(zf, info, extracted_dir, dem_only=dem_only)
                        if result:
                            extracted.append(result)

            elif lower.endswith(".tar") or lower.endswith(".tar.gz") or lower.endswith(".tgz"):
                mode = "r:gz" if (lower.endswith(".tar.gz") or lower.endswith(".tgz")) else "r:"
                with tarfile.open(archive, mode) as tf:
                    for member in tf.getmembers():
                        result = safe_extract_tar_member(tf, member, extracted_dir, dem_only=dem_only)
                        if result:
                            extracted.append(result)

            if delete_archives:
                archive.unlink()
                if emit:
                    emit(f"Удалён архив: {archive.name}")

        except Exception as e:
            errors.append(f"{archive.name}: {e}")
            if emit:
                emit(f"Ошибка распаковки {archive.name}: {e}")

    return extracted, errors, extracted_dir


def find_dem_tifs(folder):
    folder = Path(folder)
    tifs = []
    for ext in ("*.tif", "*.tiff", "*.TIF", "*.TIFF"):
        for path in folder.rglob(ext):
            if is_dem_tif_name(path.name):
                tifs.append(path)
    return sorted(set(tifs))


def build_vrt(vrt_path, tifs):
    if gdal is None:
        return False, "GDAL Python module не доступен в QGIS. VRT не создан."

    if not tifs:
        return False, "Нет DEM GeoTIFF для создания VRT."

    vrt_path = Path(vrt_path)
    vrt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if vrt_path.exists():
            vrt_path.unlink()

        ds = gdal.BuildVRT(str(vrt_path), [str(p) for p in tifs])
        if ds is None:
            return False, "gdal.BuildVRT вернул ошибку. VRT не создан."
        ds = None

        if not vrt_path.exists():
            return False, "VRT не появился на диске после BuildVRT."
        return True, str(vrt_path)

    except Exception as e:
        return False, f"Ошибка создания VRT: {e}"


def add_raster_to_project(path, name):
    layer = QgsRasterLayer(str(path), name)
    if layer.isValid():
        QgsProject.instance().addMapLayer(layer)
        return True
    return False


class DownloadTask(QgsTask):
    SEGMENT_SIZE = 32 * 1024 * 1024
    CHUNK_SIZE = 1024 * 1024

    def __init__(
        self, urls, output_dir, file_workers=2, segment_workers=4, overwrite=False,
        signals=None, shared_state=None, auto_extract=True, dem_only=True,
        delete_archives=False, auto_vrt=True, add_vrt_to_project=True
    ):
        super().__init__("ArcticDEMDownloader: скачивание тайлов", QgsTask.CanCancel)
        self.urls = urls
        self.output_dir = Path(output_dir)
        self.file_workers = int(file_workers)
        self.segment_workers = int(segment_workers)
        self.overwrite = overwrite
        self.signals = signals
        self.shared_state = shared_state or SharedDownloadState()
        self.auto_extract = auto_extract
        self.dem_only = dem_only
        self.delete_archives = delete_archives
        self.auto_vrt = auto_vrt
        self.add_vrt_to_project = add_vrt_to_project

        self.errors = []
        self.incomplete_files = []
        self.downloaded = 0
        self.skipped = 0
        self.completed = 0
        self.vrt_path = ""

        self.total_downloaded_bytes = 0
        self.file_downloaded_bytes = {}
        self.file_total_bytes = {}
        self.file_speeds = {}
        self.file_last_bytes = {}
        self.file_last_time = {}

    def run(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            total = len(self.urls)
            self.emit_message(f"Файлов к обработке: {total}")
            self.emit_message(f"Папка сохранения: {self.output_dir}")
            self.emit_message(f"Одновременных файлов: {self.file_workers}")
            self.emit_message(f"Range-потоков на файл: {self.segment_workers}")
            self.emit_message("")

            with ThreadPoolExecutor(max_workers=self.file_workers) as executor:
                future_map = {executor.submit(self.download_file, url): url for url in self.urls}

                for future in as_completed(future_map):
                    if self.isCanceled() or self.shared_state.stopped:
                        self.emit_message("Скачивание отменено пользователем.")
                        return False

                    url = future_map[future]
                    filename = filename_from_url(url)

                    try:
                        status = future.result()
                        if status == "downloaded":
                            self.downloaded += 1
                            self.emit_message(f"✓ Скачан полностью: {filename}")
                        elif status == "skipped":
                            self.skipped += 1
                            self.emit_message(f"— Пропущен, уже есть: {filename}")
                        elif status == "incomplete":
                            self.incomplete_files.append(filename)
                            self.errors.append(f"{filename} | файл скачан не полностью")
                            self.emit_message(f"× Не полностью скачан: {filename}")
                    except Exception as error:
                        self.errors.append(f"{url} | {error}")
                        self.incomplete_files.append(filename)
                        self.emit_file_progress(filename, "Ошибка", self.file_downloaded_bytes.get(filename, 0), self.file_total_bytes.get(filename, -1), 0, self.percent(filename))
                        self.emit_message(f"× Ошибка: {filename} | {error}")

                    self.completed += 1
                    self.setProgress(100 * self.completed / total)
                    self.emit_total_progress()

            if self.errors or self.incomplete_files:
                self.emit_message("Есть ошибки или неполные файлы. Распаковка/VRT не выполняются.")
                return False

            if self.auto_extract:
                self.emit_message("")
                self.emit_message("Начинаю автоматическую распаковку архивов...")
                extracted, extract_errors, extracted_dir = extract_archives(
                    self.output_dir,
                    dem_only=self.dem_only,
                    delete_archives=self.delete_archives,
                    emit=self.emit_message
                )
                if extract_errors:
                    self.errors.extend(extract_errors)
                    self.emit_message("Есть ошибки распаковки. VRT не создаётся.")
                    return False

                if self.dem_only:
                    self.emit_message(f"Извлечено DEM-файлов: {len(extracted)}")
                else:
                    self.emit_message(f"Извлечено файлов из архивов: {len(extracted)}")

            if self.auto_vrt:
                self.emit_message("")
                self.emit_message("Создаю VRT...")

                # Включаем и уже существующие DEM в папке, и только что распакованные.
                tifs = find_dem_tifs(self.output_dir)
                if not tifs:
                    self.errors.append("Не найдено DEM GeoTIFF для VRT")
                    self.emit_message("Не найдено DEM GeoTIFF для VRT.")
                    return False

                vrt = self.output_dir / "ArcticDEM.vrt"
                ok, msg = build_vrt(vrt, tifs)
                if not ok:
                    self.errors.append(msg)
                    self.emit_message(msg)
                    return False

                self.vrt_path = str(vrt)
                self.emit_message(f"VRT создан: {self.vrt_path}")

            return len(self.errors) == 0 and len(self.incomplete_files) == 0

        except Exception:
            self.errors.append(traceback.format_exc())
            self.emit_message(traceback.format_exc())
            return False

    def download_file(self, url):
        filename = filename_from_url(url)
        target = self.output_dir / filename

        if target.exists() and target.stat().st_size > 0 and not self.overwrite:
            existing_size = target.stat().st_size
            self.file_downloaded_bytes[filename] = existing_size
            self.file_total_bytes[filename] = existing_size
            self.emit_file_progress(filename, "Пропущен", existing_size, existing_size, 0, 100)
            return "skipped"

        if self.overwrite:
            self.cleanup_parts(filename)
            if target.exists():
                target.unlink()

        self.wait_if_paused(filename)
        self.emit_file_progress(filename, "Проверка сервера", 0, -1, 0, 0)

        total_size, supports_range = self.get_remote_info(url)
        self.file_total_bytes[filename] = total_size

        if total_size <= 0 or not supports_range or self.segment_workers <= 1:
            return self.download_single_stream(url, filename, total_size)

        return self.download_segmented(url, filename, total_size)

    def get_remote_info(self, url):
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "QGIS ArcticDEMDownloader"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                length = response.headers.get("Content-Length")
                length = int(length) if length and length.isdigit() else -1
                accept_ranges = response.headers.get("Accept-Ranges", "").lower()
                supports_range = "bytes" in accept_ranges
                if length > 0:
                    return length, supports_range
        except Exception:
            pass

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "QGIS ArcticDEMDownloader", "Range": "bytes=0-0"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                total = content_range.split("/")[-1]
                total = int(total) if total.isdigit() else -1
                return total, response.status == 206
            length = response.headers.get("Content-Length")
            length = int(length) if length and length.isdigit() else -1
            return length, False

    def make_segments(self, total_size):
        segments = []
        start = 0
        index = 0
        while start < total_size:
            end = min(start + self.SEGMENT_SIZE - 1, total_size - 1)
            segments.append((index, start, end))
            start = end + 1
            index += 1
        return segments

    def part_manifest_path(self, filename):
        return self.output_dir / f"{filename}.part.json"

    def segment_path(self, filename, index):
        return self.output_dir / f"{filename}.part.{index:05d}"

    def save_manifest(self, filename, url, total_size, segments):
        manifest = {
            "url": url, "filename": filename, "total_size": total_size,
            "segment_size": self.SEGMENT_SIZE,
            "segments": [{"index": i, "start": s, "end": e} for i, s, e in segments],
        }
        self.part_manifest_path(filename).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def cleanup_parts(self, filename):
        for path in self.output_dir.glob(f"{filename}.part*"):
            try:
                path.unlink()
            except Exception:
                pass

    def calculate_existing_segment_bytes(self, filename, segments):
        total = 0
        for index, start, end in segments:
            path = self.segment_path(filename, index)
            expected = end - start + 1
            if path.exists():
                total += min(path.stat().st_size, expected)
        return total

    def download_segmented(self, url, filename, total_size):
        target = self.output_dir / filename
        segments = self.make_segments(total_size)
        self.save_manifest(filename, url, total_size, segments)

        existing_bytes = self.calculate_existing_segment_bytes(filename, segments)
        self.file_downloaded_bytes[filename] = existing_bytes
        self.file_last_bytes[filename] = existing_bytes
        self.file_last_time[filename] = time.time()
        self.emit_file_progress(filename, "Скачивается Range", existing_bytes, total_size, 0, self.percent(filename))

        with ThreadPoolExecutor(max_workers=self.segment_workers) as executor:
            futures = []
            for index, start, end in segments:
                if self.is_segment_complete(filename, index, start, end):
                    continue
                futures.append(executor.submit(self.download_segment, url, filename, index, start, end, total_size))

            for future in as_completed(futures):
                if self.isCanceled() or self.shared_state.stopped:
                    return "incomplete"
                future.result()

        if not self.verify_segments(filename, segments):
            self.emit_file_progress(filename, "Не полностью", self.file_downloaded_bytes.get(filename, 0), total_size, 0, self.percent(filename))
            return "incomplete"

        self.merge_segments(filename, segments, target)

        if not target.exists() or target.stat().st_size != total_size:
            self.emit_file_progress(filename, "Не полностью", target.stat().st_size if target.exists() else 0, total_size, 0, self.percent(filename))
            return "incomplete"

        self.cleanup_parts(filename)
        self.file_downloaded_bytes[filename] = total_size
        self.file_speeds[filename] = 0
        self.emit_file_progress(filename, "Готово", total_size, total_size, 0, 100)
        self.emit_total_progress()
        return "downloaded"

    def is_segment_complete(self, filename, index, start, end):
        path = self.segment_path(filename, index)
        return path.exists() and path.stat().st_size == (end - start + 1)

    def download_segment(self, url, filename, index, start, end, total_size):
        path = self.segment_path(filename, index)
        expected = end - start + 1
        existing = path.stat().st_size if path.exists() else 0

        if existing > expected:
            path.unlink()
            existing = 0
        if existing == expected:
            return

        current_start = start + existing
        last_error = None

        for attempt in range(1, 4):
            if self.isCanceled() or self.shared_state.stopped:
                return

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "QGIS ArcticDEMDownloader",
                    "Range": f"bytes={current_start}-{end}",
                }
            )

            try:
                self.wait_if_paused(filename)
                with urllib.request.urlopen(request, timeout=90) as response:
                    if response.status not in (206, 200):
                        raise Exception(f"HTTP status {response.status}")

                    mode = "ab" if existing > 0 else "wb"
                    with open(path, mode) as file:
                        while True:
                            if self.isCanceled() or self.shared_state.stopped:
                                return
                            self.wait_if_paused(filename)

                            chunk = response.read(self.CHUNK_SIZE)
                            if not chunk:
                                break

                            file.write(chunk)
                            chunk_len = len(chunk)
                            self.file_downloaded_bytes[filename] = self.file_downloaded_bytes.get(filename, 0) + chunk_len
                            self.total_downloaded_bytes += chunk_len
                            self.update_speed(filename)

                            self.emit_file_progress(
                                filename,
                                "Пауза" if self.shared_state.is_paused(filename) else "Скачивается Range",
                                self.file_downloaded_bytes.get(filename, 0),
                                total_size,
                                self.file_speeds.get(filename, 0),
                                self.percent(filename),
                            )
                            self.emit_total_progress()

                actual = path.stat().st_size if path.exists() else 0
                if actual != expected:
                    raise Exception(f"Сегмент {index} скачан не полностью: {actual} из {expected}")
                return

            except Exception as error:
                last_error = error
                existing = path.stat().st_size if path.exists() else 0
                current_start = start + existing
                time.sleep(2 * attempt)

        raise last_error

    def verify_segments(self, filename, segments):
        for index, start, end in segments:
            path = self.segment_path(filename, index)
            expected = end - start + 1
            if not path.exists() or path.stat().st_size != expected:
                return False
        return True

    def merge_segments(self, filename, segments, target):
        self.emit_file_progress(filename, "Сборка файла", self.file_downloaded_bytes.get(filename, 0), self.file_total_bytes.get(filename, -1), 0, self.percent(filename))
        temp_target = self.output_dir / f"{filename}.merging"
        if temp_target.exists():
            temp_target.unlink()

        with open(temp_target, "wb") as out:
            for index, start, end in segments:
                path = self.segment_path(filename, index)
                with open(path, "rb") as part:
                    while True:
                        chunk = part.read(self.CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
        os.replace(str(temp_target), str(target))

    def download_single_stream(self, url, filename, total_size):
        target = self.output_dir / filename
        part = self.output_dir / f"{filename}.part"
        existing = part.stat().st_size if part.exists() else 0

        headers = {"User-Agent": "QGIS ArcticDEMDownloader"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        self.file_downloaded_bytes[filename] = existing
        self.file_last_bytes[filename] = existing
        self.file_last_time[filename] = time.time()

        last_error = None
        for attempt in range(1, 4):
            if self.isCanceled() or self.shared_state.stopped:
                return "incomplete"

            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=90) as response:
                    mode = "ab" if existing > 0 and response.status == 206 else "wb"
                    if mode == "wb":
                        self.file_downloaded_bytes[filename] = 0

                    with open(part, mode) as file:
                        while True:
                            if self.isCanceled() or self.shared_state.stopped:
                                return "incomplete"
                            self.wait_if_paused(filename)
                            chunk = response.read(self.CHUNK_SIZE)
                            if not chunk:
                                break

                            file.write(chunk)
                            chunk_len = len(chunk)
                            self.file_downloaded_bytes[filename] = self.file_downloaded_bytes.get(filename, 0) + chunk_len
                            self.total_downloaded_bytes += chunk_len
                            self.update_speed(filename)

                            downloaded = self.file_downloaded_bytes.get(filename, 0)
                            self.emit_file_progress(
                                filename,
                                "Пауза" if self.shared_state.is_paused(filename) else "Скачивается",
                                downloaded, total_size,
                                self.file_speeds.get(filename, 0),
                                self.percent(filename),
                            )
                            self.emit_total_progress()

                final_size = part.stat().st_size if part.exists() else 0
                if total_size > 0 and final_size != total_size:
                    self.emit_file_progress(filename, "Не полностью", final_size, total_size, 0, self.percent(filename))
                    return "incomplete"

                os.replace(str(part), str(target))
                target_size = target.stat().st_size if target.exists() else 0
                self.file_speeds[filename] = 0
                self.emit_file_progress(filename, "Готово", target_size, total_size if total_size > 0 else target_size, 0, 100)
                self.emit_total_progress()
                return "downloaded"

            except Exception as error:
                last_error = error
                existing = part.stat().st_size if part.exists() else 0
                headers = {"User-Agent": "QGIS ArcticDEMDownloader"}
                if existing > 0:
                    headers["Range"] = f"bytes={existing}-"
                time.sleep(2 * attempt)

        raise last_error

    def wait_if_paused(self, filename):
        while self.shared_state.is_paused(filename):
            if self.isCanceled() or self.shared_state.stopped:
                return
            self.emit_file_progress(
                filename, "Пауза",
                self.file_downloaded_bytes.get(filename, 0),
                self.file_total_bytes.get(filename, -1),
                0, self.percent(filename),
            )
            time.sleep(0.5)

    def update_speed(self, filename):
        now = time.time()
        last_time = self.file_last_time.get(filename, now)
        last_bytes = self.file_last_bytes.get(filename, self.file_downloaded_bytes.get(filename, 0))
        current_bytes = self.file_downloaded_bytes.get(filename, 0)
        elapsed = now - last_time

        if elapsed >= 0.5:
            speed = max(0, (current_bytes - last_bytes) / elapsed)
            self.file_speeds[filename] = speed
            self.file_last_time[filename] = now
            self.file_last_bytes[filename] = current_bytes

    def percent(self, filename):
        total = self.file_total_bytes.get(filename, -1)
        downloaded = self.file_downloaded_bytes.get(filename, 0)
        if total and total > 0:
            return int(min(100, max(0, downloaded * 100 / total)))
        return 0

    def emit_file_progress(self, filename, status, downloaded, total, speed, percent):
        if self.signals:
            self.signals.file_progress.emit(filename, status, int(downloaded), int(total), float(speed), int(percent))

    def emit_total_progress(self):
        total_speed = sum(v for v in self.file_speeds.values() if v > 0)
        if self.signals:
            self.signals.total_progress.emit(
                int(self.completed), int(self.downloaded), int(self.skipped),
                int(len(self.errors)), int(self.total_downloaded_bytes), float(total_speed)
            )

    def emit_message(self, text):
        QgsMessageLog.logMessage(text, PLUGIN_NAME, Qgis.Info)
        if self.signals:
            self.signals.message.emit(text)

    def finished(self, result):
        if result:
            level = Qgis.Success if not self.errors and not self.incomplete_files else Qgis.Warning
            QgsMessageLog.logMessage(
                f"Готово. Скачано: {self.downloaded}; пропущено: {self.skipped}; ошибок: {len(self.errors)}.",
                PLUGIN_NAME, level
            )
        else:
            QgsMessageLog.logMessage(
                "Скачивание/обработка завершены не полностью, остановлены или завершились с ошибкой.",
                PLUGIN_NAME, Qgis.Critical
            )

        if self.incomplete_files:
            QgsMessageLog.logMessage("Не полностью скачаны: " + ", ".join(self.incomplete_files), PLUGIN_NAME, Qgis.Critical)

        if self.errors:
            QgsMessageLog.logMessage("\\n".join(self.errors[:50]), PLUGIN_NAME, Qgis.Warning)

        if self.signals:
            self.signals.finished.emit(
                bool(result), self.downloaded, self.skipped,
                len(self.errors), self.completed, self.incomplete_files,
                self.vrt_path
            )

    def cancel(self):
        self.shared_state.stop()
        super().cancel()
        QgsMessageLog.logMessage("Скачивание отменено пользователем.", PLUGIN_NAME, Qgis.Warning)