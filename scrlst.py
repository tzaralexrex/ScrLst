import os
import sys
import subprocess
import importlib
import requests
import math
import shutil
from pathlib import Path
from datetime import timedelta
from packaging.version import parse as parse_version
from importlib.metadata import PackageNotFoundError, version as get_version

# --- Настройки ---
CHECK_VER = 1  # проверять версии пакетов на PyPI (0 = только импорт без проверки)
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}
THUMBS_PER_ROW = 4
THUMBS_PER_COL = 4
THUMB_WIDTH = 320
PADDING = 10
HEADER_HEIGHT = 60
FONT_SIZE = 20

# Режим обработки существующих скринлистов:
#   1  = всегда перезаписывать старый файл
#   0  = создавать новый файл с индексом (_1, _2, ...) если имя занято
#  -1  = если файл уже существует, пропускать этот видеофайл
OVERWRITE = 0

# Если ffmpeg и ffprobe отсутствуют - выход, они обязательны
script_dir = Path(__file__).parent.resolve()
for tool in ["ffmpeg", "ffprobe"]:
    exe_name = tool + ".exe" if os.name == "nt" else tool
    tool_path = shutil.which(tool)
    local_path = script_dir / exe_name
    if tool_path:
        continue
    elif local_path.exists():
        # Добавляем папку скрипта в PATH на время работы
        os.environ["PATH"] = str(script_dir) + os.pathsep + os.environ.get("PATH", "")
    else:
        print(f"[!] Требуется {tool} в PATH или рядом со скриптом ({exe_name}).")
        sys.exit(1)

# --- Универсальный импорт и автообновление внешних модулей ---
def import_or_update(module_name, pypi_name=None, min_version=None, force_check=False):
    """
    Импортирует модуль, при необходимости устанавливает или обновляет его до актуальной версии с PyPI.
    """
    pypi_name = pypi_name or module_name

    if not CHECK_VER and not force_check:
        try:
            return importlib.import_module(module_name)
        except ImportError:
            print(f"\n[!] Необходимый модуль {pypi_name} не установлен. Установите его вручную:\n    pip install {pypi_name}\nРабота невозможна.")
            sys.exit(1)

    print(f"Проверяю {pypi_name}", end="", flush=True)
    try:
        module = importlib.import_module(module_name)

        try:
            resp = requests.get(f"https://pypi.org/pypi/{pypi_name}/json", timeout=5)
            if resp.ok:
                latest = resp.json()["info"]["version"]
                try:
                    installed = get_version(pypi_name)
                except PackageNotFoundError:
                    installed = getattr(module, "__version__", None)

                if installed and parse_version(installed) < parse_version(latest):
                    print(f"\n[!] Доступна новая версия {pypi_name}: {installed} → {latest}. Обновляю...")
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pypi_name])
                    module = importlib.reload(module)
            print(" - OK")
        except Exception as e:
            print(f"[!] Не удалось проверить {pypi_name}: {e}")

        if min_version:
            try:
                installed = get_version(pypi_name)
            except PackageNotFoundError:
                installed = getattr(module, "__version__", None)
            if installed and parse_version(installed) < parse_version(min_version):
                print(f"\n[!] Требуется версия {min_version} для {pypi_name}, обновляю...")
                subprocess.check_call([sys.executable, "-m", "pip", "install", f"{pypi_name}>={min_version}"])
                module = importlib.reload(module)

        return module

    except ImportError:
        print(f"[!] {pypi_name} не установлен. Устанавливаю...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pypi_name])
        return importlib.import_module(module_name)

# --- Импорт Pillow через наш автообновлятор ---
PIL = import_or_update("PIL", "pillow", force_check=True)
from PIL import Image, ImageDraw, ImageFont

def run_ffprobe(video_path: Path) -> tuple[int | None, int | None, float | None]:
    """Получить метаданные видео через ffprobe."""
    cmd_stream = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ]
    try:
        out = subprocess.check_output(cmd_stream, stderr=subprocess.DEVNULL).decode().strip().split("\n")
    except subprocess.CalledProcessError as e:
        print(f"[!] ffprobe не удалось выполнить для {video_path}: {e}")
        return None, None, None

    width = height = dur = None
    if len(out) >= 3:
        try:
            width = int(out[0])
            height = int(out[1])
        except ValueError:
            pass
        try:
            dur = float(out[2])
        except ValueError:
            dur = None

    # Если длительность не получена — пробуем через формат
    if not dur:
        cmd_fmt = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        try:
            out2 = subprocess.check_output(cmd_fmt, stderr=subprocess.DEVNULL).decode().strip()
            dur = float(out2)
        except Exception:
            dur = None

    return width, height, dur

def format_size(bytes_size: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} PB"

def resolve_output_path(base_path: Path) -> Path | None:
    """
    Возвращает итоговый путь для скринлиста с учётом настройки OVERWRITE.
    base_path: ожидаемое имя файла (например video.jpg)
    """
    if OVERWRITE == 1:
        return base_path
    elif OVERWRITE == -1:
        if base_path.exists():
            print(f"[!] Скринлист {base_path.name} уже существует, пропускаю.")
            return None
        return base_path
    else:  # OVERWRITE == 0
        candidate = base_path
        idx = 1
        while candidate.exists():
            candidate = candidate.with_stem(f"{base_path.stem}_{idx}")
            idx += 1
        return candidate

def create_thumbnail(video_path: Path, output_path: Path) -> None:
    """Создать скринлист для видео."""
    width, height, duration = run_ffprobe(video_path)
    if not duration:
        print(f"[!] Не удалось обработать {video_path}")
        return

    total_shots = THUMBS_PER_ROW * THUMBS_PER_COL
    step = duration / (total_shots + 1)
    timestamps = [step * (i+1) for i in range(total_shots)]

    temp_dir = Path("_thumbs_temp")
    temp_dir.mkdir(exist_ok=True)

    images = []
    for i, ts in enumerate(timestamps):
        ts_str = str(timedelta(seconds=int(ts)))
        img_file = temp_dir / f"shot_{i:03d}.jpg"
        cmd = [
            "ffmpeg", "-ss", str(ts), "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2", "-y", str(img_file)
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            print(f"[!] ffmpeg не удалось извлечь кадр {i} для {video_path}")
            continue
        if img_file.exists():
            with Image.open(img_file) as img:
                img = img.convert("RGB")
                img = img.resize((THUMB_WIDTH, int(THUMB_WIDTH * img.height / img.width)))
                draw = ImageDraw.Draw(img)
                draw.text((5, 5), ts_str, fill="white")
                images.append(img)

    if not images:
        print(f"[!] Не удалось извлечь кадры из {video_path}")
        return

    thumb_h = images[0].height
    total_w = THUMBS_PER_ROW * THUMB_WIDTH + (THUMBS_PER_ROW+1) * PADDING
    total_h = HEADER_HEIGHT + THUMBS_PER_COL * thumb_h + (THUMBS_PER_COL+1) * PADDING

    sheet = Image.new("RGB", (total_w, total_h), "black")
    draw = ImageDraw.Draw(sheet)

    # --- Шапка ---
    stat = video_path.stat()
    header_text = f"{video_path.name} | {width}x{height} | {format_size(stat.st_size)} | {str(timedelta(seconds=int(duration)))}"
    try:
        font = ImageFont.truetype("arial.ttf", FONT_SIZE)
    except:
        font = ImageFont.load_default()
    draw.text((PADDING, PADDING), header_text, fill="white", font=font)

    # --- Вставка миниатюр ---
    for idx, img in enumerate(images):
        row, col = divmod(idx, THUMBS_PER_ROW)
        x = PADDING + col * (THUMB_WIDTH + PADDING)
        y = HEADER_HEIGHT + PADDING + row * (thumb_h + PADDING)
        sheet.paste(img, (x, y))

    sheet.save(output_path, "JPEG", quality=90)
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

def main():
    args = sys.argv[1:]
    recursive = False
    file_arg = None

    # Проверяем аргументы командной строки
    for arg in args:
        if arg in ("-r", "--recursive"):
            recursive = True
        elif not arg.startswith("-"):
            file_arg = arg

    if file_arg:
        if recursive:
            print("[!] Ключ -r (или --recursive) игнорируется при обработке одного файла.")
        # Обработка одного файла, имя передано в командной строке
        file = Path(file_arg)
        if not file.exists() or not file.is_file():
            print(f"[!] Файл {file} не найден.")
            return
        if file.suffix.lower() not in VIDEO_EXTS:
            print(f"[!] Файл {file} не является поддерживаемым видео.")
            return
        out_file = file.with_suffix(".jpg")
        resolved = resolve_output_path(out_file)
        if not resolved:
            return  # пропуск по правилу OVERWRITE
        print(f"[+] Обрабатываю {file.name} → {resolved.name}")
        create_thumbnail(file, resolved)
    else:
        # Обработка всех файлов в папке (и подпапках, если recursive)
        folder = Path(".")
        if recursive:
            files = folder.rglob("*")
        else:
            files = folder.iterdir()
        for file in files:
            if file.is_file() and file.suffix.lower() in VIDEO_EXTS:
                out_file = file.with_suffix(".jpg")
                resolved = resolve_output_path(out_file)
                if not resolved:
                    continue  # пропуск по правилу OVERWRITE
                print(f"[+] Обрабатываю {file.relative_to(folder)} → {resolved.name}")
                create_thumbnail(file, resolved)

if __name__ == "__main__":
    main()
