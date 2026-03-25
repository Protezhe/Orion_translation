#!/usr/bin/env python3
"""
Парковое радио — Flask веб-сервер с GUI в браузере.

Запуск:
    python3 park_radio_server.py

Открыть в браузере:
    http://localhost:8080          — на этом компьютере
    http://<IP-адрес>:8080         — с любого устройства в локалке
"""
from __future__ import annotations

import json
import queue as _queue
import random
import sys
import threading
import time
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
#  python-vlc
# ──────────────────────────────────────────────────────────────────
try:
    import vlc
except ImportError:
    print("Установите: pip install python-vlc")
    sys.exit(1)

from flask import Flask, jsonify, request, render_template

# ──────────────────────────────────────────────────────────────────
#  Константы
# ──────────────────────────────────────────────────────────────────
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma"}
BASE_DIR   = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULTS: dict = {
    "music_dir": "music",
    "announcements_dir": "announcements",
    "music_volume": 70,
    "announcements_volume": 90,
    "songs_between_announcements": 3,
    "working_hours": {"start": "09:00", "end": "22:00"},
    "scheduled_announcements": [],
    # Формат: [{"file": "promo.mp3", "times": ["10:00", "14:30"]}]
}

# ──────────────────────────────────────────────────────────────────
#  Конфиг
# ──────────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────
#  Файлы
# ──────────────────────────────────────────────────────────────────
def scan_audio(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    files = [
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    ]
    files.sort(key=lambda f: f.name.lower())
    return files


# ──────────────────────────────────────────────────────────────────
#  Движок плеера (python-vlc)
# ──────────────────────────────────────────────────────────────────
class RadioPlayer:
    """
    Плеер на базе libVLC. Фоновый поток управляет очередью треков.
    Все публичные методы потокобезопасны.
    """

    def __init__(self, config: dict):
        self._lock   = threading.Lock()
        self.config  = config

        self._music_vol: int = config["music_volume"]          # 0–100
        self._ann_vol:   int = config["announcements_volume"]  # 0–100
        self.songs_between: int = config["songs_between_announcements"]

        music_dir = BASE_DIR / config["music_dir"]
        ann_dir   = BASE_DIR / config["announcements_dir"]
        self.music_files: list[Path] = scan_audio(music_dir)
        self.ann_files:   list[Path] = scan_audio(ann_dir)

        # VLC
        self._vlc = vlc.Instance("--no-video", "--quiet")
        self._mp  = self._vlc.media_player_new()

        # Очередь
        self._queue: list[tuple[Path, str]] = []
        self._song_counter: int = 0

        # Состояние
        self.current_name: str  = "—"
        self.current_type: str  = ""     # "music" | "ann"
        self.is_playing:   bool = False
        self.is_paused:    bool = False
        self.is_fading:    bool = False
        self.log: list[dict]    = []

        # Очередь плановых роликов (из Scheduler → в _loop)
        self._sched_queue: _queue.Queue = _queue.Queue()

        # Флаги управления
        self._stop_ev  = threading.Event()
        self._skip_ev  = threading.Event()
        self._pause_ev = threading.Event()
        self._pause_ev.set()  # изначально не на паузе

    # ── Очередь ───────────────────────────────────────────────────

    def _rescan(self):
        """Пересканирует папки — подхватывает новые и удалённые файлы."""
        self.music_files = scan_audio(BASE_DIR / self.config["music_dir"])
        self.ann_files   = scan_audio(BASE_DIR / self.config["announcements_dir"])

    def _refill(self):
        self._rescan()
        if not self.music_files:
            return
        batch = list(self.music_files)
        random.shuffle(batch)
        for track in batch:
            if (self.songs_between < 20
                    and self._song_counter > 0
                    and self._song_counter % self.songs_between == 0
                    and self.ann_files):
                self._queue.append((random.choice(self.ann_files), "ann"))
            self._queue.append((track, "music"))
            self._song_counter += 1

    def songs_until_ann(self) -> int | None:
        """Сколько музыкальных треков до следующего объявления в очереди."""
        if not self.ann_files or self.songs_between >= 20:
            return None
        count = 0
        for _, t in self._queue:
            if t == "ann":
                return count
            count += 1
        return count

    # ── Управление ────────────────────────────────────────────────

    def start(self):
        self._stop_ev.clear()
        self.is_playing = True
        threading.Thread(target=self._loop, daemon=True, name="RadioLoop").start()

    def _fade_out(self, duration: float, on_complete):
        """Плавно снижает громкость до 0 за duration секунд, затем вызывает on_complete."""
        def _run():
            start_vol = self._mp.audio_get_volume()
            steps = max(1, int(duration * 25))   # ~25 шагов/сек
            delay = duration / steps
            for i in range(steps, -1, -1):
                if self._stop_ev.is_set():
                    return
                self._mp.audio_set_volume(max(0, int(start_vol * i / steps)))
                time.sleep(delay)
            on_complete()
        threading.Thread(target=_run, daemon=True, name="FadeOut").start()

    def pause(self):
        with self._lock:
            if self.is_paused or self.is_fading:
                return
            self.is_fading = True

        def _do():
            self._mp.pause()
            with self._lock:
                self.is_paused = True
                self.is_fading = False
            self._pause_ev.clear()

        self._fade_out(3.0, _do)

    def resume(self):
        with self._lock:
            if not self.is_paused:
                return
            self._mp.pause()  # VLC: pause() toggles
            # Восстанавливаем громкость (была снижена до 0 при фейде)
            vol = self._music_vol if self.current_type == "music" else self._ann_vol
            self._mp.audio_set_volume(vol)
            self.is_paused = False
        self._pause_ev.set()

    def toggle_pause(self):
        if self.is_paused:
            self.resume()
        else:
            self.pause()

    def skip(self):
        with self._lock:
            if self.is_fading:
                # Уже идёт фейд — пропускаем немедленно
                self.is_fading = False
                self._skip_ev.set()
                self._mp.stop()
                return
            self.is_fading = True

        def _do():
            with self._lock:
                self.is_fading = False
            self._skip_ev.set()
            self._mp.stop()

        self._fade_out(3.0, _do)

    def stop(self):
        self._stop_ev.set()
        self._pause_ev.set()
        self._mp.stop()

    # ── Громкость ─────────────────────────────────────────────────

    @property
    def music_vol(self) -> int:
        return self._music_vol

    @music_vol.setter
    def music_vol(self, val: int):
        self._music_vol = max(0, min(100, val))
        if self.current_type == "music":
            self._mp.audio_set_volume(self._music_vol)

    @property
    def ann_vol(self) -> int:
        return self._ann_vol

    @ann_vol.setter
    def ann_vol(self, val: int):
        self._ann_vol = max(0, min(100, val))
        if self.current_type in ("ann", "scheduled"):
            self._mp.audio_set_volume(self._ann_vol)

    # ── Позиция ───────────────────────────────────────────────────

    def elapsed_str(self) -> str:
        ms = self._mp.get_time()
        if ms < 0:
            return "0:00"
        sec = ms // 1000
        return f"{sec // 60}:{sec % 60:02d}"

    def duration_str(self) -> str:
        ms = self._mp.get_length()
        if ms <= 0:
            return ""
        sec = ms // 1000
        return f"{sec // 60}:{sec % 60:02d}"

    def interrupt_with(self, path: Path):
        """Поставить плановый ролик — будет сыгран как только закончится текущий фейд."""
        self._sched_queue.put(path)

    def _fade_out_sync(self, duration: float):
        """Синхронный фейд (блокирует поток _loop). Восстанавливать громкость — вызывающий."""
        start_vol = self._mp.audio_get_volume()
        if start_vol <= 0:
            return
        steps = max(1, int(duration * 20))
        delay = duration / steps
        for i in range(steps, -1, -1):
            if self._stop_ev.is_set():
                return
            self._mp.audio_set_volume(max(0, int(start_vol * i / steps)))
            time.sleep(delay)

    def _play_scheduled_now(self, path: Path):
        """Воспроизводит плановый ролик синхронно внутри потока _loop."""
        saved_name = self.current_name
        saved_type = self.current_type
        self.current_name = path.stem
        self.current_type = "scheduled"

        try:
            media = self._vlc.media_new(str(path))
            self._mp.set_media(media)
            self._mp.play()
            time.sleep(0.2)
            self._mp.audio_set_volume(self._ann_vol)
        except Exception as e:
            self._add_log(f"ОШИБКА ролика {path.name}: {e}", "err")
            self.current_name = saved_name
            self.current_type = saved_type
            return

        self._add_log(path.stem, "scheduled")
        time.sleep(0.15)

        # Ждём окончания ролика
        while True:
            if self._stop_ev.is_set():
                self._mp.stop()
                break
            if self._is_finished():
                break
            time.sleep(0.1)

        self.current_name = saved_name
        self.current_type = saved_type

    # ── Основной цикл ─────────────────────────────────────────────

    def _is_finished(self) -> bool:
        state = self._mp.get_state()
        return state in (vlc.State.Ended, vlc.State.Error, vlc.State.Stopped,
                         vlc.State.NothingSpecial)

    def _loop(self):
        while not self._stop_ev.is_set():
            # Ждём снятия паузы
            self._pause_ev.wait()
            if self._stop_ev.is_set():
                break

            # Пополняем очередь
            if len(self._queue) < 5:
                self._refill()
            if not self._queue:
                time.sleep(0.5)
                continue

            path, ttype = self._queue.pop(0)
            self._skip_ev.clear()

            with self._lock:
                self.current_name = path.stem
                self.current_type = ttype

            vol = self._music_vol if ttype == "music" else self._ann_vol

            try:
                media = self._vlc.media_new(str(path))
                self._mp.set_media(media)
                self._mp.play()
                # Ставим громкость ПОСЛЕ play() — VLC сбрасывает её при смене медиа
                time.sleep(0.15)
                self._mp.audio_set_volume(vol)
            except Exception as e:
                self._add_log(f"ОШИБКА: {path.name}: {e}", "err")
                continue

            self._add_log(path.stem, ttype)

            # Дополнительная пауза для стабилизации
            time.sleep(0.15)

            # Ждём окончания трека
            while True:
                if self._stop_ev.is_set():
                    self._mp.stop()
                    return
                if self._skip_ev.is_set():
                    self._mp.stop()
                    break
                # Плановый ролик прерывает текущий трек
                try:
                    sched_path = self._sched_queue.get_nowait()
                    self._fade_out_sync(3.0)
                    self._mp.stop()
                    self._play_scheduled_now(sched_path)
                    # Восстанавливаем громкость для следующего трека
                    vol = self._music_vol if ttype == "music" else self._ann_vol
                    self._mp.audio_set_volume(vol)
                    break
                except _queue.Empty:
                    pass
                if not self.is_paused and self._is_finished():
                    break
                time.sleep(0.1)

    def _add_log(self, name: str, ttype: str):
        entry = {
            "name": name,
            "type": ttype,
            "time": time.strftime("%H:%M:%S"),
        }
        self.log.insert(0, entry)
        if len(self.log) > 50:
            self.log.pop()

    # ── Снимок состояния для API ──────────────────────────────────

    def snapshot(self) -> dict:
        n = self.songs_until_ann()
        if n is None:
            next_ann = None
        elif n == 0:
            next_ann = "сейчас"
        elif n == 1:
            next_ann = "через 1 песню"
        else:
            next_ann = f"через {n} песен"

        # Убедимся что очередь заполнена перед отдачей
        if len(self._queue) < 5:
            self._refill()

        queue_preview = [
            {"name": p.stem, "type": t}
            for p, t in self._queue[:30]
        ]

        return {
            "track":       self.current_name,
            "type":        self.current_type,
            "elapsed":     self.elapsed_str(),
            "duration":    self.duration_str(),
            "paused":      self.is_paused,
            "fading":      self.is_fading,
            "music_vol":   self._music_vol,
            "ann_vol":     self._ann_vol,
            "next_ann":    next_ann,
            "music_count":   len(self.music_files),
            "ann_count":     len(self.ann_files),
            "songs_between": self.songs_between,
            "log":           self.log[:20],
            "queue":         queue_preview,
            "schedule":         schedule_info(self.config),
            "next_scheduled":   next_scheduled_info(self.config),
        }


# ──────────────────────────────────────────────────────────────────
#  Расписание
# ──────────────────────────────────────────────────────────────────
def parse_hhmm(s: str) -> int:
    """Переводит 'HH:MM' в минуты от полуночи."""
    h, m = map(int, s.strip().split(":"))
    return h * 60 + m


def schedule_info(config: dict) -> dict:
    wh = config["working_hours"]
    start_str = wh["start"]
    end_str   = wh["end"]

    start_mins = parse_hhmm(start_str)
    end_mins   = parse_hhmm(end_str)

    t = time.localtime()
    now_mins = t.tm_hour * 60 + t.tm_min

    active = start_mins <= now_mins < end_mins

    if active:
        diff       = end_mins - now_mins
        next_event = "Выключение"
        next_time  = end_str
    else:
        if now_mins < start_mins:
            diff = start_mins - now_mins
        else:
            diff = (24 * 60 - now_mins) + start_mins
        next_event = "Включение"
        next_time  = start_str

    h, m = divmod(diff, 60)
    if h > 0:
        next_in = f"через {h} ч {m} мин" if m else f"через {h} ч"
    else:
        next_in = f"через {m} мин"

    return {
        "start":           start_str,
        "end":             end_str,
        "active":          active,
        "next_event":      next_event,
        "next_event_time": next_time,
        "next_event_in":   next_in,
    }


def next_scheduled_info(config: dict) -> dict | None:
    """Возвращает ближайший плановый ролик с учётом дней недели."""
    now      = time.localtime()
    now_mins = now.tm_hour * 60 + now.tm_min
    now_dow  = now.tm_wday   # 0=Пн, 6=Вс

    DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    best_diff = float("inf")
    best: dict | None = None

    for ann in config.get("scheduled_announcements", []):
        days = ann.get("days", [])
        active_days = days if days else list(range(7))

        for t in ann.get("times", []):
            try:
                t_mins = parse_hhmm(t)
            except Exception:
                continue
            # Ищем ближайший подходящий день
            for delta_day in range(7):
                candidate_dow = (now_dow + delta_day) % 7
                if candidate_dow not in active_days:
                    continue
                diff = t_mins - now_mins + delta_day * 24 * 60
                if delta_day == 0 and diff <= 0:
                    continue   # уже прошло сегодня
                if diff < best_diff:
                    best_diff = diff
                    h, m = divmod(diff, 60)
                    day_str = "сегодня" if delta_day == 0 else (
                              "завтра"  if delta_day == 1 else DOW_RU[candidate_dow])
                    in_str  = (f"через {h} ч {m} мин" if h and m else
                               f"через {h} ч"         if h        else
                               f"через {m} мин")
                    best = {"file": ann["file"], "time": t,
                            "day": day_str, "in": in_str}
                break  # нашли ближайший день для этого времени

    return best


class Scheduler:
    """
    Следит за расписанием: запускает/останавливает плеер по рабочим часам
    и запускает плановые ролики в заданное время.
    Проверяет каждые 10 секунд.
    """

    def __init__(self, player_ref: RadioPlayer, config_ref: dict):
        self._player     = player_ref
        self._cfg        = config_ref
        self._played_today: set[str] = set()
        self._last_date: str         = ""

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="Scheduler").start()

    def _loop(self):
        time.sleep(5)
        tick = 0
        while True:
            try:
                self._check_scheduled_anns()
                if tick % 3 == 0:          # каждые 30 сек
                    self._check_working_hours()
                tick += 1
            except Exception as e:
                print(f"[Расписание] Ошибка: {e}")
            time.sleep(10)

    def _check_working_hours(self):
        info = schedule_info(self._cfg)
        if info["active"] and not self._player.is_playing:
            print(f"[Расписание] {time.strftime('%H:%M')} — рабочее время, запускаю плеер")
            self._player.start()
        elif not info["active"] and self._player.is_playing:
            print(f"[Расписание] {time.strftime('%H:%M')} — конец рабочего времени, останавливаю")
            self._player.stop()

    def _check_scheduled_anns(self):
        today    = time.strftime("%Y-%m-%d")
        now_str  = time.strftime("%H:%M")
        today_dow = time.localtime().tm_wday   # 0=Пн, 6=Вс
        ann_dir  = BASE_DIR / self._cfg.get("announcements_dir", "announcements")

        # Сброс в новый день
        if self._last_date != today:
            self._played_today.clear()
            self._last_date = today

        if not self._player.is_playing or self._player.is_paused:
            return

        for ann in self._cfg.get("scheduled_announcements", []):
            fname    = ann.get("file", "").strip()
            filepath = ann_dir / fname
            if not filepath.exists():
                continue
            # Проверяем день недели (пустой список = каждый день)
            days = ann.get("days", [])
            if days and today_dow not in days:
                continue
            for t in ann.get("times", []):
                key = f"{fname}_{today}_{t}"
                if t == now_str and key not in self._played_today:
                    self._played_today.add(key)
                    self._player.interrupt_with(filepath)
                    print(f"[Расписание] {now_str} — плановый ролик: {fname}")

    def _check(self):
        """Совместимость — немедленная проверка рабочих часов."""
        self._check_working_hours()


# ──────────────────────────────────────────────────────────────────
#  Flask
# ──────────────────────────────────────────────────────────────────
app       = Flask(__name__)
cfg       = load_config()
player    = RadioPlayer(cfg)
scheduler = Scheduler(player, cfg)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(player.snapshot())


@app.route("/api/pause", methods=["POST"])
def api_pause():
    player.toggle_pause()
    return jsonify({"paused": player.is_paused})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    player.skip()
    return jsonify({"ok": True})


@app.route("/api/schedule", methods=["GET"])
def api_schedule_get():
    return jsonify(schedule_info(cfg))


@app.route("/api/schedule", methods=["POST"])
def api_schedule_set():
    data = request.get_json(force=True)
    start = data.get("start", "").strip()
    end   = data.get("end",   "").strip()

    # Валидация формата HH:MM
    import re
    pattern = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    if not pattern.match(start) or not pattern.match(end):
        return jsonify({"error": "Формат времени: HH:MM (00:00–23:59)"}), 400
    if parse_hhmm(start) >= parse_hhmm(end):
        return jsonify({"error": "Время начала должно быть раньше окончания"}), 400

    cfg["working_hours"]["start"] = start
    cfg["working_hours"]["end"]   = end
    save_config(cfg)

    # Немедленная проверка расписания
    scheduler._check()

    return jsonify(schedule_info(cfg))


@app.route("/api/scheduled", methods=["GET"])
def api_scheduled_get():
    ann_dir   = BASE_DIR / cfg.get("announcements_dir", "announcements")
    available = [f.name for f in scan_audio(ann_dir)]
    return jsonify({
        "announcements":   cfg.get("scheduled_announcements", []),
        "available_files": available,
        "next":            next_scheduled_info(cfg),
    })


@app.route("/api/scheduled", methods=["POST"])
def api_scheduled_set():
    data = request.get_json(force=True)
    import re
    time_re = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

    validated = []
    for ann in data.get("announcements", []):
        fname = str(ann.get("file", "")).strip()
        if not fname:
            continue
        times = sorted({t for t in ann.get("times", []) if time_re.match(str(t))})
        days  = sorted({int(d) for d in ann.get("days", []) if str(d).isdigit() and 0 <= int(d) <= 6})
        validated.append({"file": fname, "times": times, "days": days})

    cfg["scheduled_announcements"] = validated
    save_config(cfg)
    return jsonify({"ok": True, "next": next_scheduled_info(cfg)})



@app.route("/api/volume", methods=["POST"])
def api_volume():
    data  = request.get_json(force=True)
    kind  = data.get("type")
    value = max(0, min(100, int(data.get("value", 70))))

    if kind == "music":
        player.music_vol = value
        cfg["music_volume"] = value
    elif kind == "ann":
        player.ann_vol = value
        cfg["announcements_volume"] = value
    else:
        return jsonify({"error": "unknown type"}), 400

    save_config(cfg)
    return jsonify({"ok": True, "value": value})


# ──────────────────────────────────────────────────────────────────
#  Точка входа
# ──────────────────────────────────────────────────────────────────
def get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    if not player.music_files:
        print("[ПРЕДУПРЕЖДЕНИЕ] Папка music/ пуста или не найдена!")

    # Запускаем плеер только если сейчас рабочее время
    if schedule_info(cfg)["active"]:
        player.start()
    else:
        wh = cfg["working_hours"]
        print(f"[Расписание] Сейчас не рабочее время ({wh['start']}–{wh['end']}). Плеер запустится по расписанию.")

    scheduler.start()

    ip = get_local_ip()
    print("\n" + "=" * 52)
    print("  ПАРКОВОЕ РАДИО — веб-интерфейс")
    print("=" * 52)
    print(f"  Музыка:       {len(player.music_files)} файлов")
    print(f"  Объявления:   {len(player.ann_files)} файлов")
    print()
    print(f"  На этом ПК:   http://localhost:8080")
    print(f"  В локалке:    http://{ip}:8080")
    print("=" * 52 + "\n")

    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)


if __name__ == "__main__":
    main()
