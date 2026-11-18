"""Генерирует аудиофайлы с озвучкой всех фраз таймера.

Запуск (только на хосте macOS — нужны `say` и `ffmpeg`, в Docker-песочнице их нет):

    python3 tools/make_audio.py                     # голос Milena
    python3 tools/make_audio.py --voice Yuri
    python3 tools/make_audio.py --list-voices       # русские голоса системы
    python3 tools/make_audio.py --dry-run           # только показать фразы

Результат: src/audio/<голос>/<ключ>.m4a + src/audio/<голос>/phrases.json
(ключ → текст) + src/audio/voices.json (список всех готовых голосов). Уже готовые файлы пропускаются, --force перегенерирует.

Фразы повторяют то, что говорит src/index.html:
  start_N     «Таймер на N минуту/минуты/минут»   N = 1..60
  left_min_N  «Осталось N минуты/минут» («Осталась … одна минута») N = 1..60
  left_30s    «Осталось тридцать секунд»
  count_N     «N»                                 N = 1..10
  resume      «Продолжаю»
  pause       «Пауза»
  done        «Время вышло!»
Числа записаны словами с правильным родом («одна минута», «одну минуту»),
чтобы синтезатор не читал «один минуту».
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "audio"
DEFAULT_VOICE = "Milena"

# Обрезка тишины: срезаем начало, разворачиваем, срезаем «начало» (т.е. конец), разворачиваем назад.
SILENCE = "silenceremove=start_periods=1:start_threshold={th}:start_silence=0.02"
FILTER = "{s},areverse,{s},areverse"

UNITS = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
         "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят"]
FEM = {"nom": {"один": "одна", "два": "две"}, "acc": {"один": "одну", "два": "две"}}


def words(n, case=None):
    """Число 1..69 словами; case='nom'|'acc' — женский род (для «минута»)."""
    if 10 <= n < 20:
        return TEENS[n - 10]
    tens, unit = TENS[n // 10], UNITS[n % 10]
    if case and unit:
        unit = FEM[case].get(unit, unit)
    return " ".join(p for p in (tens, unit) if p)


def plural(n, one, few, many):
    m10, m100 = n % 10, n % 100
    if m10 == 1 and m100 != 11:
        return one
    if 2 <= m10 <= 4 and not 12 <= m100 <= 14:
        return few
    return many


def phrases():
    p = {}
    for n in range(1, 61):
        p[f"start_{n}"] = f"Таймер на {words(n, 'acc')} {plural(n, 'минуту', 'минуты', 'минут')}"
    for n in range(1, 61):
        verb = plural(n, "Осталась", "Осталось", "Осталось")  # «осталась одна минута»
        p[f"left_min_{n}"] = f"{verb} {words(n, 'nom')} {plural(n, 'минута', 'минуты', 'минут')}"
    p["left_30s"] = "Осталось тридцать секунд"
    for n in range(1, 11):
        p[f"count_{n}"] = words(n).capitalize()
    p["resume"] = "Продолжаю"
    p["pause"] = "Пауза"
    p["done"] = "Время вышло!"
    return p


def list_voices():
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if re.search(r"\bru[_-]RU\b", line):
            print(line)


def slug(voice):
    return re.sub(r"[^\w.-]+", "_", voice).strip("_") or "voice"


def render(key, text, voice, out_dir, threshold, bitrate, force):
    dst = out_dir / f"{key}.m4a"
    if dst.exists() and not force:
        return key, "skip"
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "raw.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        s = SILENCE.format(th=threshold)
        part = dst.with_suffix(".part.m4a")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(aiff),
             "-af", FILTER.format(s=s), "-ac", "1", "-c:a", "aac", "-b:a", bitrate,
             "-movflags", "+faststart", str(part)],
            check=True,
        )
        part.replace(dst)
    return key, "ok"


def write_voices_index(root):
    """audio/voices.json — список готовых голосов для выпадающего списка в index.html."""
    voices = []
    for meta in sorted(root.glob("*/phrases.json")):
        try:
            name = json.loads(meta.read_text(encoding="utf-8")).get("voice") or meta.parent.name
        except (OSError, ValueError):
            continue
        voices.append({"id": meta.parent.name, "name": name})
    (root / "voices.json").write_text(
        json.dumps({"voices": voices}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--voice", default=DEFAULT_VOICE, help=f"голос macOS say (по умолчанию {DEFAULT_VOICE})")
    ap.add_argument("--out", type=Path, default=OUT, help="корневая папка (по умолчанию src/audio)")
    ap.add_argument("--threshold", default="-45dB", help="порог тишины для ffmpeg (по умолчанию -45dB)")
    ap.add_argument("--bitrate", default="64k", help="битрейт AAC (по умолчанию 64k)")
    ap.add_argument("--jobs", type=int, default=4, help="параллельных процессов")
    ap.add_argument("--force", action="store_true", help="перегенерировать существующие файлы")
    ap.add_argument("--list-voices", action="store_true", help="показать русские голоса и выйти")
    ap.add_argument("--dry-run", action="store_true", help="только вывести фразы")
    args = ap.parse_args()

    p = phrases()
    if args.dry_run:
        for k, t in p.items():
            print(f"{k:12} {t}")
        print(f"всего: {len(p)}")
        return

    missing = [t for t in ("say", "ffmpeg") if not shutil.which(t)]
    if missing:
        sys.exit(f"Не найдено: {', '.join(missing)}. Нужен macOS и ffmpeg (brew install ffmpeg).")
    if args.list_voices:
        list_voices()
        return

    # Проверяем, что голос существует: say с неизвестным голосом молча берёт системный.
    known = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    names = {re.split(r"\s{2,}", l.strip())[0] for l in known.splitlines() if l.strip()}
    if args.voice not in names:
        sys.exit(f"Голос «{args.voice}» не найден. Список: --list-voices "
                 "(новые голоса: Системные настройки → Универсальный доступ → Устная речь).")

    out_dir = args.out / slug(args.voice)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Голос {args.voice} → {out_dir} ({len(p)} фраз)")

    failed = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {ex.submit(render, k, t, args.voice, out_dir, args.threshold, args.bitrate, args.force): k
                for k, t in p.items()}
        for i, f in enumerate(futs, 1):
            k = futs[f]
            try:
                _, status = f.result()
                print(f"[{i:3}/{len(p)}] {status:4} {k}")
            except subprocess.CalledProcessError as e:
                failed.append(k)
                print(f"[{i:3}/{len(p)}] FAIL {k}: {e}", file=sys.stderr)

    (out_dir / "phrases.json").write_text(
        json.dumps({"voice": args.voice, "phrases": p}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_voices_index(args.out)
    if failed:
        sys.exit(f"Ошибки: {len(failed)} — {', '.join(failed)}")
    print("Готово.")


if __name__ == "__main__":
    main()
