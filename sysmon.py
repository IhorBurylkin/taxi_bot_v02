#!/usr/bin/env python3
import os
import time
import sys
import re
import signal
import threading
from collections import namedtuple

RESET          = "\033[0m"
FG_BLACK       = "\033[30m"
BG_WHITE       = "\033[47m"
SAVE_CURSOR    = "\033[s"
RESTORE_CURSOR = "\033[u"
CLEAR_LINE     = "\033[2K"

Prev = namedtuple('Prev', (
    'cpu_idle', 'cpu_total',
    'disk_read', 'disk_write',
    'net_rx', 'net_tx'
))

def read_cpu_times():
    with open('/proc/stat', 'r') as f:
        fields = f.readline().split()
    vals = list(map(int, fields[1:]))
    idle = vals[3] + vals[4]  # idle + iowait
    total = sum(vals)
    return idle, total

def read_mem_percent():
    info = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            key, val = line.split(':')[0], int(line.split()[1])
            info[key] = val
    total     = info.get('MemTotal', 0)
    available = info.get('MemAvailable',
                         info.get('MemFree', 0)
                       + info.get('Buffers', 0)
                       + info.get('Cached', 0))
    used = total - available
    return used * 100.0 / total if total else 0.0

def read_disk_usage_percent(path='/'):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used  = total - avail
    return used * 100.0 / total if total else 0.0

def read_disk_io():
    read_sectors = write_sectors = 0
    with open('/proc/diskstats', 'r') as f:
        for line in f:
            parts = line.split()
            name  = parts[2]
            # суммируем по дискам, исключая партиции
            if re.match(r'^(sd[a-z]|hd[a-z]|nvme\d+n\d+)$', name):
                read_sectors  += int(parts[5])
                write_sectors += int(parts[9])
    # Сектора (512 байт) -> MB
    return read_sectors * 512 / 1024**2, write_sectors * 512 / 1024**2

def read_net_dev():
    rx = tx = 0
    with open('/proc/net/dev', 'r') as f:
        f.readline(); f.readline()
        for line in f:
            iface, data = line.split(':', 1)
            iface = iface.strip()
            if iface == 'lo':
                continue
            vals = list(map(int, data.split()))
            rx += vals[0]  # bytes
            tx += vals[8]  # bytes
    return rx / 1024, tx / 1024  # KB

def move_to_bottom():
    rows = os.get_terminal_size().lines
    return f"\033[{rows};1H"

def _loop(stop: threading.Event, interval: float = 1.0, use_colors: bool = True):
    # если не TTY — тихо выходим
    if not sys.stdout.isatty():
        return

    idle0, total0 = read_cpu_times()
    rd0, wr0     = read_disk_io()
    rx0, tx0     = read_net_dev()
    prev = Prev(idle0, total0, rd0, wr0, rx0, tx0)
    prev_time = time.time()

    def style(s: str) -> str:
        return f"{BG_WHITE}{FG_BLACK}{s}{RESET}" if use_colors else s

    try:
        while not stop.is_set():
            now = time.time()
            dt  = max(1e-6, now - prev_time)

            # CPU
            idle1, total1 = read_cpu_times()
            d_idle  = idle1  - prev.cpu_idle
            d_total = total1 - prev.cpu_total
            cpu_pct = (1.0 - d_idle / d_total) * 100.0 if d_total else 0.0

            # RAM / DISK
            ram_pct  = read_mem_percent()
            disk_pct = read_disk_usage_percent('/')

            # I/O
            rd1, wr1 = read_disk_io()
            rd_rate  = (rd1 - prev.disk_read) / dt
            wr_rate  = (wr1 - prev.disk_write) / dt

            # NET
            rx1, tx1 = read_net_dev()
            rx_rate  = (rx1 - prev.net_rx) / dt
            tx_rate  = (tx1 - prev.net_tx) / dt

            prev = Prev(idle1, total1, rd1, wr1, rx1, tx1)
            prev_time = now

            line = (
                f"CPU:{cpu_pct:5.1f}% "
                f"RAM:{ram_pct:5.1f}% "
                f"DISK:{disk_pct:5.1f}% "
                f"I/O R:{rd_rate:6.2f}MB/s W:{wr_rate:6.2f}MB/s "
                f"NET ↑{tx_rate:6.2f}KB/s ↓{rx_rate:6.2f}KB/s"
            )

            sys.stdout.write(
                SAVE_CURSOR +
                move_to_bottom() + "\r" + CLEAR_LINE +
                style(line) +
                RESTORE_CURSOR
            )
            sys.stdout.flush()

            # спим с возможностью быстрого пробуждения
            stop.wait(interval)
    except Exception as e:
        print(f"\n[sysmon error] {e}", file=sys.stderr)
    finally:
        # на выходе — вернём курсор как был и перенесём строку
        try:
            sys.stdout.write(RESTORE_CURSOR + "\n")
            sys.stdout.flush()
        except Exception:
            pass

def start_in_thread(interval: float = 1.0, use_colors: bool = True):
    """
    Запускает монитор в отдельном потоке-демоне.
    Возвращает (stop_event, thread).
    """
    stop = threading.Event()
    t = threading.Thread(
        target=_loop, args=(stop, interval, use_colors),
        name="sysmon", daemon=True
    )
    t.start()
    return stop, t

# Режим одиночного запуска: удобно для «быстрого теста»
def _handle_sig(signum, frame):
    raise SystemExit

if __name__ == '__main__':
    # Корректно завершаем по Ctrl+C и SIGTERM
    signal.signal(signal.SIGINT, _handle_sig)
    try:
        signal.signal(signal.SIGTERM, _handle_sig)
    except Exception:
        pass
    stop = threading.Event()
    try:
        _loop(stop, interval=1.0, use_colors=True)
    except SystemExit:
        pass
