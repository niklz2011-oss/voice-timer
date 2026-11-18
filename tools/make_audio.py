"""Генерирует аудиофайлы с озвучкой всех фраз таймера.

Запуск (только на хосте macOS — нужны `say` и `ffmpeg`/`ffprobe`, в Docker-песочнице их нет):

    python3 tools/make_audio.py                     # голос Milena
    python3 tools/make_audio.py --voice Yuri
    python3 tools/make_audio.py --list-voices       # русские голоса системы
    python3 tools/make_audio.py --dry-run           # только показать фразы

Результат: src/audio/<голос>/<ключ>.m4a + src/audio/<голос>/phrases.json
(ключ → текст) + src/audio/voices.json (список всех готовых голосов).
Уже готовые файлы пропускаются, --force перегенерирует. Файлы фраз, которых больше
нет в списке (старые версии), удаляются.

Фразы повторяют то, что говорит src/index.html:
  count_N     «N»                    N = 1..60 — счёт каждую секунду, каждое число короче 0,9 с
  minute_N    «вторая минута» …      N = 2..60 — объявление в начале новой минуты
  pause       «Пауза»
  done        «Время вышло!»
Если число звучит дольше 0,9 с, оно перезаписывается с более быстрым темпом речи.
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
MAX_COUNT_S = 0.9                         # число должно уложиться в свою секунду
RATES = [None, 200, 220, 240, 260, 280, 300]   # None — темп голоса по умолчанию

# Обрезка тишины: срезаем начало, разворачиваем, срезаем «начало» (т.е. конец), разворачиваем назад.
SILENCE = "silenceremove=start_periods=1:start_threshold={th}:start_silence=0.02"
FILTER = "{s},areverse,{s},areverse"

UNITS = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
         "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят"]

# Порядковые числительные женского рода («вторая минута»).
ORD_UNITS = ["", "первая", "вторая", "третья", "четвёртая", "пятая",
             "шестая", "седьмая", "восьмая", "девятая"]
ORD_TEENS = ["десятая", "одиннадцатая", "двенадцатая", "тринадцатая", "четырнадцатая",
             "пятнадцатая", "шестнадцатая", "семнадцатая", "восемнадцатая", "девятнадцатая"]
ORD_TENS = ["", "", "двадцатая", "тридцатая", "сороковая", "пятидесятая", "шестидесятая"]


def words(n):
    """Количественное числительное 1..69 словами."""
    if 10 <= n < 20:
        return TEENS[n - 10]
    return " ".join(p for p in (TENS[n // 10], UNITS[n % 10]) if p)


def ordinal_fem(n):
    """Порядковое числительное 1..69, женский род: 21 → «двадцать первая»."""
    if 10 <= n < 20:
        return ORD_TEENS[n - 10]
    tens, unit = n // 10, n % 10
    if not unit:
        return ORD_TENS[tens]
    return " ".join(p for p in (TENS[tens], ORD_UNITS[unit]) if p)


def phrases():
    p = {}
    for n in range(1, 61):
        p[f"count_{n}"] = words(n).capitalize()
    for n in range(2, 61):
        p[f"minute_{n}"] = f"{ordinal_fem(n).capitalize()} минута"
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


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def render(key, text, voice, out_dir, threshold, bitrate, force):
    dst = out_dir / f"{key}.m4a"
    limit = MAX_COUNT_S if key.startswith("count_") else None
    if dst.exists() and not force and (limit is None or duration(dst) < limit):
        return key, "skip", duration(dst)
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "raw.aiff"
        part = Path(tmp) / "out.m4a"
        s = SILENCE.format(th=threshold)
        for rate in (RATES if limit else RATES[:1]):
            cmd = ["say", "-v", voice, "-o", str(aiff)]
            if rate:
                cmd += ["-r", str(rate)]
            subprocess.run(cmd + [text], check=True)
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(aiff),
                 "-af", FILTER.format(s=s), "-ac", "1", "-c:a", "aac", "-b:a", bitrate,
                 "-movflags", "+faststart", str(part)],
                check=True,
            )
            d = duration(part)
            if limit is None or d < limit:
                break
        else:
            raise RuntimeError(f"{key}: {d:.2f} с — не удалось уложиться в {limit} с")
        shutil.move(str(part), dst)
    return key, f"ok{'' if not rate else f' r={rate}'}", d


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

    missing = [t for t in ("say", "ffmpeg", "ffprobe") if not shutil.which(t)]
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
                _, status, d = f.result()
                print(f"[{i:3}/{len(p)}] {d:5.2f} с  {status:9} {k}  «{p[k]}»")
            except (subprocess.CalledProcessError, RuntimeError, ValueError) as e:
                failed.append(k)
                print(f"[{i:3}/{len(p)}] FAIL {k}: {e}", file=sys.stderr)

    # Удаляем файлы фраз, которых больше нет (старые объявления и т.п.).
    for old in out_dir.glob("*.m4a"):
        if old.stem not in p:
            old.unlink()
            print(f"удалён устаревший {old.name}")

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
