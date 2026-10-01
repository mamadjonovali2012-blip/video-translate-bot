"""Качественный перевод видео: аудио -> STT -> перевод -> TTS -> монтаж.

Полный пайплайн выполняется на сервере клиента (см. README «Боевой запуск»):
  video.mp4
    -> ffmpeg  извлечь аудио (pcm 16k mono), узнать длительность
    -> whisper распознать речь -> [(start, end, text)]
    -> deepl / google  перевести текст каждого сегмента
    -> edge-tts озвучить переведённый текст на целевом языке
    -> ffmpeg  смонтировать новую дорожку из TTS-фрагментов (с паузами),
               приглушить/перекрыть оригинал, склеить с видео
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.config import TranslateConfig

# https://github.com/openai/whisper/blob/main/whisper/tokenizer.py
LANG_CODES = {
    "ru", "en", "de", "fr", "es", "it", "pt", "uk", "pl", "tr", "zh", "ja",
    "ko", "ar", "hi", "kk", "az", "be", "ka", "hy", "uz", "lt", "lv", "et",
}


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Capabilities:
    ffmpeg: bool = False
    ffprobe: bool = False
    whisper: bool = False
    deepl: bool = False
    translator: bool = False
    tts: bool = False

    @property
    def full(self) -> bool:
        return all([self.ffmpeg, self.ffprobe, self.whisper,
                    (self.deepl or self.translator), self.tts])

    def missing(self) -> list[str]:
        out = []
        if not self.ffmpeg:
            out.append("ffmpeg")
        if not self.ffprobe:
            out.append("ffprobe")
        if not self.whisper:
            out.append("faster-whisper")
        if not (self.deepl or self.translator):
            out.append("deep-translator/deepl")
        if not self.tts:
            out.append("edge-tts")
        return out


def detect_capabilities() -> Capabilities:
    """Лёгкий (без тяжёлых импортов) аудит того, что можно запустить."""
    import importlib.util

    spec = importlib.util.find_spec
    cap = Capabilities(
        ffmpeg=shutil.which("ffmpeg") is not None,
        ffprobe=shutil.which("ffprobe") is not None,
        whisper=spec("faster_whisper") is not None,
        deepl=spec("deepl") is not None,
        translator=spec("deep_translator") is not None,
        tts=spec("edge_tts") is not None,
    )
    return cap


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            "Команда не выполнена: " + " ".join(cmd) + "\n" +
            (res.stderr or res.stdout)[-800:]
        )
    return res.stdout


# --------------------------------------------------------------------------
# Аудио / видео
# --------------------------------------------------------------------------

def ffprobe_duration(path: Path) -> float:
    out = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def extract_audio(video: Path, out_audio: Path) -> None:
    """Извлечь монофонический аудио-трек 16k (для whisper)."""
    _run([
        "ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", str(out_audio),
    ])


# --------------------------------------------------------------------------
# STT
# --------------------------------------------------------------------------

def _load_whisper(device: str, compute: str):
    from faster_whisper import WhisperModel  # noqa: PLC0415
    if device == "auto":
        try:
            import torch  # noqa: PLC0415
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
    if compute == "auto":
        compute = "int8"
    return WhisperModel("base", device=device, compute_type=compute)


def transcribe(audio: Path, device: str, compute: str) -> list[Segment]:
    model = _load_whisper(device, compute)
    segments, _info = model.transcribe(str(audio), vad_filter=True)
    out: list[Segment] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            out.append(Segment(float(seg.start), float(seg.end), text))
    return out


# --------------------------------------------------------------------------
# Перевод
# --------------------------------------------------------------------------

def translate_text(text: str, target: str, prefer_deepl: bool = True) -> str:
    """Перевести отдельную строку. Возвращает пустую строку при ошибке ключа."""
    if not text.strip():
        return ""
    if prefer_deepl and os.getenv("VT_DEEPL_KEY"):
        try:
            import deepl  # noqa: PLC0415
            tr = deepl.Translator(os.environ["VT_DEEPL_KEY"])
            return tr.translate_text(text, target_lang=target).text
        except Exception:
            # падаем на google-бэкенд
            pass
    from deep_translator import GoogleTranslator  # noqa: PLC0415
    # target: нормализуем iso-код в то, что ждёт google (регистр не важен)
    return GoogleTranslator(source="auto", target=target).translate(text)


# --------------------------------------------------------------------------
# TTS + монтаж
# --------------------------------------------------------------------------

async def tts_save(text: str, voice: str, out_path: Path) -> None:
    import edge_tts  # noqa: PLC0415
    comm = edge_tts.Communicate(text, voice=voice)
    await comm.save(str(out_path))


def _delays_ms(segs: list[Segment]) -> list[float]:
    """Начало каждого TTS-сегмента относительно начала видео (мс)."""
    return [max(0, int(s.start * 1000)) for s in segs]


def assemble_dubbed(
    video: Path,
    segs: list[Segment],
    tts_files: list[Path],
    target_lang: str,
    out_path: Path,
    original_volume: float = 0.18,
    tts_volume: float = 1.0,
) -> None:
    """Смонтаж оригинальное видео + оригинал приглушён + TTS-дубляж.

    Переведённая речь кладётся в те же временные точки, где звучал оригинал,
    поверх подпорченного (приглушённого) исходного звука — так получается
    замена голоса без сдвига общей картины.
    """
    if not tts_files:
        raise RuntimeError("Нет озвученных сегментов для монтажа.")

    # 1) аудио одного клипа = цепочка: каждый фрагмент задержан на start сегмента.
    #    Между фразами используем тишину из самого клипа; паузы сохраняем.
    adelay_parts = [
        f"[{i}:a]adelay={delay}|{delay},volume={tts_volume}[d{i}]"
        for i, (delay) in enumerate(_delays_ms(segs))
    ]
    # собрать микс: amix всех дорожек (нормализация выключена -> веса равны)
    inputs = "".join(f"[d{i}]" for i in range(len(tts_files)))
    mix = f"{inputs}amix=inputs={len(tts_files)}:normalize=0,format=s16le[tts]"

    # 2) оригинальная дорожка приглушается
    av = f"volume={original_volume}"

    # 3) финальный фильтр: [orig] приглушить, [tts] поверх, amix
    #    ffmpeg amix двух дорожек
    filter_complex = ",".join(
        adelay_parts + [mix, f"[0:a]{av}[a0]", "[tts][a0]amix=inputs=2:normalize=0[aout]"]
    )

    inputs_cmd: list[str] = []
    for path in tts_files:
        inputs_cmd += ["-i", str(path)]
    # индекс 0 = видео (у него и есть аудио), дальше tts-клипы
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video),
        *inputs_cmd,
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(out_path),
    ]
    _run(cmd)
    _ = target_lang


async def translate_video(
    video_path: Path,
    target_lang: str,
    cfg: TranslateConfig,
    workdir: Path,
) -> Path:
    """Полный пайплайн: возвращает путь к переведённому файлу."""
    caps = detect_capabilities()
    if not caps.full:
        raise RuntimeError("Нет компонентов: " + ", ".join(caps.missing()))

    tmp = workdir / "pipe"
    tmp.mkdir(parents=True, exist_ok=True)

    audio = tmp / "audio.wav"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, extract_audio, video_path, audio)

    segs = await loop.run_in_executor(
        None, transcribe, audio, cfg.device, cfg.compute_type)
    if not segs:
        raise RuntimeError("Речь не распознана (пустое или тихое видео).")

    loop = asyncio.get_running_loop()
    translated = await asyncio.gather(*[
        loop.run_in_executor(
            None, translate_text, s.text, target_lang,
            os.getenv("VT_DEEPL_KEY") not in (None, ""))
        for s in segs
    ])

    # TTS пофразово, последовательно (быстро, стабильно)
    tts_files: list[Path] = []
    for txt in translated:
        txt = (txt or "").strip()
        if not txt:
            continue
        p = tmp / f"seg_{len(tts_files):04d}.mp3"
        await tts_save(txt, cfg.tts_voice, p)
        length = await loop.run_in_executor(None, ffprobe_duration, p)
        if length <= 0:
            continue
        tts_files.append(p)

    if not tts_files:
        raise RuntimeError("Перевод пуст — нечего озвучивать.")

    out = workdir / f"translated_{target_lang}.mp4"
    await loop.run_in_executor(
        None, assemble_dubbed, video_path, segs[:len(tts_files)], tts_files,
        target_lang, out, 0.18, 1.0)
    return out