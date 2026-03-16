#!/usr/bin/env python3
"""
Парковое радио — автоматическая трансляция музыки и объявлений через VLC.

Принцип работы:
  1. Синхронизирует файлы с Яндекс.Диска (по токену)
  2. Генерирует M3U-плейлист на весь день (музыка + объявления, разная громкость)
  3. Запускает VLC с этим плейлистом — VLC играет самостоятельно
  4. Периодически проверяет изменения на диске и пересоздаёт плейлист

Если скрипт упадёт — VLC продолжит играть текущий плейлист.

Структура на Яндекс.Диске (orion_music/translation/):
  ├── music/          — фоновая музыка
  ├── announcements/  — объявления
  └── config.json     — настройки (громкость, интервалы)
"""

import json
import os
import random
import signal
import subprocess
import sys
import time
import hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma"}
BASE_DIR = Path(__file__).parent.resolve()
PLAYLIST_PATH = BASE_DIR / "playlist.m3u"
API_BASE = "https://cloud-api.yandex.net/v1/disk"


def _find_vlc() -> str:
    """Находит путь к VLC. На macOS бинарник внутри .app бандла."""
    import shutil
    vlc = shutil.which("vlc")
    if vlc:
        return vlc
    mac_vlc = "/Applications/VLC.app/Contents/MacOS/VLC"
    if os.path.isfile(mac_vlc):
        return mac_vlc
    return "vlc"


VLC_PATH = _find_vlc()


# ─── Яндекс.Диск API ─────────────────────────────────────────────────────────

class YandexDiskSync:
    """Синхронизация файлов с Яндекс.Диска через REST API."""

    def __init__(self, token: str, remote_base: str):
        self.token = token
        self.remote_base = remote_base.rstrip("/")
        self._headers = {"Authorization": f"OAuth {token}"}

    def _api_get(self, url: str) -> dict | None:
        req = Request(url, headers=self._headers)
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                return None
            print(f"[ДИСК] Ошибка API {e.code}: {e.reason}")
            return None
        except (URLError, OSError) as e:
            print(f"[ДИСК] Ошибка сети: {e}")
            return None

    def list_files(self, remote_folder: str) -> list[dict]:
        path = f"{self.remote_base}/{remote_folder}"
        encoded = quote(path, safe="/:")
        url = f"{API_BASE}/resources?path={encoded}&limit=1000"
        data = self._api_get(url)
        if not data or "_embedded" not in data:
            return []
        items = data["_embedded"].get("items", [])
        return [
            {"name": item["name"], "md5": item.get("md5", ""), "size": item.get("size", 0)}
            for item in items
            if item["type"] == "file"
        ]

    def download_file(self, remote_path: str, local_path: Path) -> bool:
        encoded = quote(remote_path, safe="/:")
        url = f"{API_BASE}/resources/download?path={encoded}"
        data = self._api_get(url)
        if not data or "href" not in data:
            print(f"[ДИСК] Не удалось получить ссылку: {remote_path}")
            return False
        download_url = data["href"]
        req = Request(download_url)
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with urlopen(req, timeout=120) as resp, open(local_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            return True
        except (URLError, OSError) as e:
            print(f"[ДИСК] Ошибка скачивания {remote_path}: {e}")
            return False

    def get_remote_config(self) -> dict | None:
        remote_path = f"{self.remote_base}/config.json"
        encoded = quote(remote_path, safe="/:")
        url = f"{API_BASE}/resources/download?path={encoded}"
        data = self._api_get(url)
        if not data or "href" not in data:
            return None
        try:
            req = Request(data["href"])
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def sync_folder(self, remote_folder: str, local_dir: Path) -> tuple[int, int]:
        remote_files = self.list_files(remote_folder)
        remote_names = {f["name"]: f for f in remote_files}
        local_dir.mkdir(parents=True, exist_ok=True)
        local_files = {f.name: f for f in local_dir.iterdir() if f.is_file()}
        added = 0
        removed = 0

        for name, info in remote_names.items():
            local_path = local_dir / name
            need_download = False
            if name not in local_files:
                need_download = True
            elif info.get("md5"):
                local_md5 = self._file_md5(local_path)
                if local_md5 != info["md5"]:
                    need_download = True
            elif info.get("size", 0) != local_path.stat().st_size:
                need_download = True

            if need_download:
                remote_path = f"{self.remote_base}/{remote_folder}/{name}"
                print(f"[ДИСК] Скачиваю: {remote_folder}/{name}")
                if self.download_file(remote_path, local_path):
                    added += 1

        for name in local_files:
            if name not in remote_names:
                (local_dir / name).unlink()
                print(f"[ДИСК] Удалён локально: {remote_folder}/{name}")
                removed += 1

        return added, removed

    @staticmethod
    def _file_md5(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()


# ─── Конфигурация ────────────────────────────────────────────────────────────

def load_local_config() -> dict:
    config_path = BASE_DIR / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ОШИБКА] Не удалось прочитать config.json: {e}")
        return {}


def apply_defaults(cfg: dict) -> dict:
    defaults = {
        "music_dir": "music",
        "announcements_dir": "announcements",
        "music_volume": 70,
        "announcements_volume": 90,
        "songs_between_announcements": 3,
        "sync_interval_sec": 60,
        "working_hours": {"start": "09:00", "end": "22:00"},
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


# ─── Сканирование файлов ─────────────────────────────────────────────────────

def scan_audio_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    files = [
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    ]
    files.sort(key=lambda f: f.name.lower())
    return files


# ─── Генерация плейлиста ──────────────────────────────────────────────────────

def generate_playlist(
    music_files: list[Path],
    announcement_files: list[Path],
    music_volume: int,
    announcements_volume: int,
    songs_between: int,
    target_hours: int = 14,
) -> str:
    """
    Генерирует M3U-плейлист на день.
    Объявления вставляются каждые N песен.
    Громкость задаётся через #EXTVLCOPT:gain=X (1.0 = 100%).
    """
    if not music_files:
        return "#EXTM3U\n"

    music_gain = max(0.0, min(2.0, music_volume / 100.0))
    ann_gain = max(0.0, min(2.0, announcements_volume / 100.0))

    # Перемешиваем музыку, делаем достаточно повторов на весь день
    # Средний трек ~3.5 мин → ~17 треков/час → target_hours * 17
    tracks_needed = target_hours * 17
    shuffled_music = []
    while len(shuffled_music) < tracks_needed:
        batch = list(music_files)
        random.shuffle(batch)
        shuffled_music.extend(batch)

    lines = ["#EXTM3U", ""]
    song_count = 0

    for track in shuffled_music:
        # Вставить объявление
        if song_count > 0 and song_count % songs_between == 0 and announcement_files:
            ann = random.choice(announcement_files)
            lines.append(f"#EXTINF:-1,{ann.stem}")
            lines.append(f"#EXTVLCOPT:gain={ann_gain:.2f}")
            lines.append(str(ann))
            lines.append("")

        lines.append(f"#EXTINF:-1,{track.stem}")
        lines.append(f"#EXTVLCOPT:gain={music_gain:.2f}")
        lines.append(str(track))
        lines.append("")
        song_count += 1

    return "\n".join(lines)


def write_playlist(config: dict) -> int:
    """Генерирует и сохраняет плейлист. Возвращает количество треков."""
    music_dir = BASE_DIR / config["music_dir"]
    ann_dir = BASE_DIR / config["announcements_dir"]
    music_files = scan_audio_files(music_dir)
    ann_files = scan_audio_files(ann_dir)

    wh = config.get("working_hours", {})
    start_h = int(wh.get("start", "09:00").split(":")[0])
    end_h = int(wh.get("end", "22:00").split(":")[0])
    target_hours = max(1, end_h - start_h)

    content = generate_playlist(
        music_files=music_files,
        announcement_files=ann_files,
        music_volume=config.get("music_volume", 70),
        announcements_volume=config.get("announcements_volume", 90),
        songs_between=config.get("songs_between_announcements", 3),
        target_hours=target_hours,
    )

    PLAYLIST_PATH.write_text(content, encoding="utf-8")
    total = content.count("#EXTINF:")
    print(f"[ПЛЕЙЛИСТ] Сгенерирован: {total} треков (музыка: {len(music_files)}, объявления: {len(ann_files)})")
    return total


# ─── VLC-процесс ──────────────────────────────────────────────────────────────

class VLCProcess:
    """Управляет VLC-процессом, играющим плейлист."""

    def __init__(self):
        self._process: subprocess.Popen | None = None

    def start(self, playlist_path: Path):
        """Запускает VLC с плейлистом."""
        self.stop()
        cmd = [
            VLC_PATH,
            "--no-video",
            "--play-and-exit",
            "--no-loop",
            "--no-repeat",
            str(playlist_path),
        ]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[VLC] Запущен (PID: {self._process.pid})")
        except FileNotFoundError:
            print("[ОШИБКА] VLC не найден! Установите VLC:")
            print("  macOS: brew install --cask vlc")
            print("  Linux: sudo apt install vlc")
            sys.exit(1)

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self):
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            print("[VLC] Остановлен")
        self._process = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None


# ─── Основной цикл ───────────────────────────────────────────────────────────

class ParkRadio:
    def __init__(self):
        self.config = apply_defaults(load_local_config())
        self.disk: YandexDiskSync | None = None
        self.vlc = VLCProcess()
        self._running = True
        self._last_playlist_hash = ""

        yd = self.config.get("yandex_disk", {})
        if yd.get("token"):
            self.disk = YandexDiskSync(yd["token"], yd.get("remote_path", "disk:/"))

    def start(self):
        print("=" * 60)
        print("  ПАРКОВОЕ РАДИО — запуск трансляции")
        print("=" * 60)

        # Первичная синхронизация
        if self.disk:
            print("\n[ДИСК] Первичная синхронизация с Яндекс.Диском...")
            self._do_sync()
        else:
            print("[ДИСК] Токен не задан — работаю с локальными файлами")

        # Генерация плейлиста и запуск VLC
        write_playlist(self.config)
        self._last_playlist_hash = self._playlist_inputs_hash()
        self.vlc.start(PLAYLIST_PATH)
        self._print_status()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        print("[ИНФО] Скрипт следит за изменениями. Если скрипт упадёт — VLC продолжит играть.")
        print(f"[ИНФО] VLC PID: {self.vlc.pid} (можно убить отдельно: kill {self.vlc.pid})")
        print()

        # Цикл мониторинга
        interval = self.config.get("sync_interval_sec", 60)
        while self._running:
            time.sleep(interval)
            if not self._running:
                break

            # Синхронизация с диском
            if self.disk:
                self._do_sync()

            # Проверяем, нужно ли пересоздать плейлист
            new_hash = self._playlist_inputs_hash()
            if new_hash != self._last_playlist_hash:
                print("[ОБНОВЛЕНИЕ] Обнаружены изменения, пересоздаю плейлист...")
                write_playlist(self.config)
                self._last_playlist_hash = new_hash
                self.vlc.start(PLAYLIST_PATH)

            # Если VLC умер — перезапускаем
            if not self.vlc.is_running():
                print("[VLC] Плейлист закончился или VLC упал. Пересоздаю и перезапускаю...")
                write_playlist(self.config)
                self.vlc.start(PLAYLIST_PATH)

        self.vlc.stop()
        print("\n[СТОП] Трансляция завершена.")

    def stop(self):
        self._running = False

    def _handle_signal(self, signum, frame):
        print("\n[СИГНАЛ] Завершение...")
        self.stop()

    def _do_sync(self):
        if not self.disk:
            return
        music_dir = self.config.get("music_dir", "music")
        ann_dir = self.config.get("announcements_dir", "announcements")

        m_add, m_del = self.disk.sync_folder(music_dir, BASE_DIR / music_dir)
        a_add, a_del = self.disk.sync_folder(ann_dir, BASE_DIR / ann_dir)

        if m_add or m_del or a_add or a_del:
            print(f"[ДИСК] Синхронизация: музыка +{m_add}/-{m_del}, объявления +{a_add}/-{a_del}")

        # Конфиг с диска
        remote_cfg = self.disk.get_remote_config()
        if remote_cfg:
            remote_cfg["yandex_disk"] = self.config.get("yandex_disk", {})
            self.config = apply_defaults(remote_cfg)

    def _playlist_inputs_hash(self) -> str:
        """Хэш входных данных для плейлиста (файлы + громкости)."""
        music_dir = BASE_DIR / self.config["music_dir"]
        ann_dir = BASE_DIR / self.config["announcements_dir"]
        music = sorted(f.name for f in scan_audio_files(music_dir))
        anns = sorted(f.name for f in scan_audio_files(ann_dir))
        data = json.dumps({
            "music": music,
            "announcements": anns,
            "music_volume": self.config.get("music_volume"),
            "announcements_volume": self.config.get("announcements_volume"),
            "songs_between": self.config.get("songs_between_announcements"),
        }, sort_keys=True)
        return hashlib.md5(data.encode()).hexdigest()

    def _print_status(self):
        music_files = scan_audio_files(BASE_DIR / self.config["music_dir"])
        ann_files = scan_audio_files(BASE_DIR / self.config["announcements_dir"])
        print(f"\n  Музыка:       {self.config['music_dir']}/ ({len(music_files)} файлов)")
        print(f"  Объявления:   {self.config['announcements_dir']}/ ({len(ann_files)} файлов)")
        print(f"  Громкость музыки:      {self.config['music_volume']}%")
        print(f"  Громкость объявлений:  {self.config['announcements_volume']}%")
        wh = self.config.get("working_hours", {})
        print(f"  Часы работы:  {wh.get('start', '00:00')} — {wh.get('end', '23:59')}")
        print(f"  Объявление:   каждые {self.config['songs_between_announcements']} песен")
        if self.disk:
            print(f"  Яндекс.Диск:  синхронизация каждые {self.config.get('sync_interval_sec', 60)} сек.")
        print()


# ─── Точка входа ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    radio = ParkRadio()
    radio.start()
