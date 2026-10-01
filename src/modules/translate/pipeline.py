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

# edge-tts: голос по умолчанию для целевого языка (нейроголоса Microsoft).
# Если языка нет в карте — используется конфиг по умолчанию.
LANG_VOICES = {
    "ru": "ru-RU-DmitryNeural",
    "en": "en-US-ChristopherNeural",
    "de": "de-DE-ConradNeural",
    "fr": "fr-FR-HenriNeural",
    "es": "es-ES-AlvaroNeural",
    "it": "it-IT-DiegoNeural",
    "pt": "pt-BR-AntonioNeural",
    "uk": "uk-UA-OstapNeural",
    "pl": "pl-PL-MarekNeural",
    "tr": "tr-TR-AhmetNeural",
    "zh": "zh-CN-YunxiNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "ar": "ar-SA-HamedNeural",
    "hi": "hi-IN-MadhurNeural",
    "kk": "kk-KZ-AigulNeural",
    "az": "az-AZ-BabekNeural",
    "be": "be-BY-DmitryNeural",
    "ka": "ka-GE-GiorgiNeural",
    "hy": "hy-AM-HaykNeural",
    "uz": "uz-UZ-MadinaNeural",
    "lt": "lt-LT-LeonasNeural",
    "lv": "lv-LV-EveritaNeural",
    "et": "et-EE-AnuNeural",
    "rg": "ru-RU-DmitryNeural",  # псевдоним, на всякий случай
}


def voice_for(lang: str, fallback: str) -> str:
    return LANG_VOICES.get(lang, fallback)


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


def _read_wav_float32(path: Path):
    """Прочитать 16-bit mono WAV в float32 [-1, 1] через stdlib.

    ffmpeg всегда выдаёт PCM s16le mono 16k, поэтому стандартный
    `wave`-модуль надёжно декодирует без PyAV (который конфликтует с
    более новыми версиями faster-whisper).
    """
    import wave

    import numpy as np  # noqa: PLC0415

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise RuntimeError("Ожидается 16-bit PCM (ffmpeg -ac 1 -ar 16000).")
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return arr


def transcribe(audio: Path, device: str, compute: str) -> list[Segment]:
    model = _load_whisper(device, compute)
    audio_arr = _read_wav_float32(audio)
    segments, _info = model.transcribe(audio_arr, vad_filter=True)
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
    # google транслитерирует «source=auto»; регистр целевого кода не важен
    return GoogleTranslator(source="auto", target=target).translate(text)


def translate_text_with_retry(text: str, target: str,
                              prefer_deepl: bool = True,
                              attempts: int = 4,
                              base_delay: float = 1.0) -> str:
    """Перевод с повторными попытками при временных (сетевых) ошибках."""
    import time  # noqa: PLC0415

    from deep_translator.exceptions import TooManyRequests  # noqa: PLC0415

    last = None
    for attempt in range(attempts):
        try:
            return translate_text(text, target, prefer_deepl)
        except TooManyRequests as e:
            last = e
            time.sleep(base_delay * (attempt + 1) * 2)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(base_delay)
    raise RuntimeError(f"Перевод не удался после {attempts} попыток: {last}")


# --------------------------------------------------------------------------
# TTS + монтаж
# --------------------------------------------------------------------------

async def tts_save(text: str, voice: str, out_path: Path,
                   attempts: int = 6) -> None:
    """Синтез речи в mp3. edge-tts свободен, но бывают таймауты -> ретраи."""
    import asyncio as _aio  # noqa: PLC0415

    last: Exception | None = None
    for i in range(attempts):
        try:
            import edge_tts  # noqa: PLC0415
            out_path.parent.mkdir(parents=True, exist_ok=True)
            comm = edge_tts.Communicate(text, voice=voice)
            await comm.save(str(out_path))
            return
        except Exception as e:  # noqa: BLE001
            last = e
            await _aio.sleep(3 * (i + 1))
    raise RuntimeError(f"TTS не удался после {attempts} попыток: {last}")


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
    bitrate: str = "128k",
) -> None:
    """Смонтаж оригинальное видео + оригинал приглушён + TTS-дубляж.

    Переведённая речь кладётся в те же временные точки, где звучал оригинал,
    поверх подпорченного (приглушённого) исходного звука — так получается
    замена голоса без сдвига общей картины.
    """
    if not tts_files:
        raise RuntimeError("Нет озвученных сегментов для монтажа.")

    video_idx = len(tts_files)  # индекс видео среди входов (после tts-клипов)

    # 1) каждый TTS-клип задерживаем на начало соответствующего сегмента
    filter_parts = [
        f"[{i}:a]adelay={delay}|{delay},volume={tts_volume}[d{i}]"
        for i, delay in enumerate(_delays_ms(segs))
    ]
    # 2) смешиваем все TTS-дорожки в один трек
    tts_ins = "".join(f"[d{i}]" for i in range(len(tts_files)))
    filter_parts.append(
        f"{tts_ins}amix=inputs={len(tts_files)}:normalize=0:dropout_transition=0"
        f",aformat=sample_fmts=s16:channel_layouts=mono[tts]"
    )
    # 3) оригинальную дорожку приглушаем
    filter_parts.append(
        f"[{video_idx}:a]volume={original_volume}[orig]"
    )
    # 4) финальный микс TTS + едва слышный оригинал
    filter_parts.append(
        "[tts][orig]amix=inputs=2:normalize=0:dropout_transition=0[aout]"
    )

    filter_complex = ",".join(filter_parts)

    inputs_cmd: list[str] = []
    for path in tts_files:
        inputs_cmd += ["-i", str(path)]
    inputs_cmd += ["-i", str(video)]

    cmd = [
        "ffmpeg", "-y",
        *inputs_cmd,
        "-filter_complex", filter_complex,
        "-map", f"{video_idx}:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", bitrate,
        "-shortest",
        str(out_path),
    ]
    _run(cmd)


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
    prefer_deepl = os.getenv("VT_DEEPL_KEY") not in (None, "")
    # переводим последовательно с ретраями, чтобы не словить rate-limit
    translated: list[str] = []
    for s in segs:
        if not s.text.strip():
            translated.append("")
            continue
        t = await loop.run_in_executor(
            None, translate_text_with_retry, s.text, target_lang, prefer_deepl)
        translated.append(t)

    # TTS пофразово, последовательно (быстро, стабильно)
    tts_voice = voice_for(target_lang, cfg.tts_voice)
    tts_files: list[Path] = []
    for txt in translated:
        txt = (txt or "").strip()
        if not txt:
            continue
        p = tmp / f"seg_{len(tts_files):04d}.mp3"
        await tts_save(txt, tts_voice, p)
        length = await loop.run_in_executor(None, ffprobe_duration, p)
        if length <= 0:
            continue
        tts_files.append(p)

    if not tts_files:
        raise RuntimeError("Перевод пуст — нечего озвучивать.")

    out = workdir / f"translated_{target_lang}.mp4"
    await loop.run_in_executor(
        None, assemble_dubbed, video_path, segs[:len(tts_files)], tts_files,
        target_lang, out, 0.18, 1.0, cfg.out_bitrate)
    return out