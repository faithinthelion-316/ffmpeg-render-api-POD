from pydantic import BaseModel

import os
import uuid
import shutil
import subprocess
import base64
import re
import unicodedata
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

app = FastAPI()

BASE_DIR = "/tmp/ffmpeg_render"
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
VIDEO_DIR = os.path.join(BASE_DIR, "video")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
IMAGE_DIR = os.path.join(BASE_DIR, "images")
CLIPS_DIR = os.path.join(BASE_DIR, "clips")

MUSIC_FILE = "/app/music/background.mp3"  # optional for POD; render works without it

END_TAIL_DURATION = 0.0

# Video-tail policy:
# Keep the music-only ending after the voice, but do not create that tail by
# freezing the last frame of the final scene. Long visual padding is blocked.
MIN_SCENE_DURATION = 0.5
MAX_CLONE_PAD_PER_SCENE = 0.12

HOOK_CARD_START = 0.00
HOOK_CARD_END = 0.00

HOOK_WORD_1_START = 0.12
HOOK_WORD_2_START = 0.24
HOOK_WORD_3_START = 0.42

REFERENCE_START_TIME = 6.0

CTA_CARD_DURATION = 0.00

TRUTH_PUNCH_DURATION = 0.0

AI_VIDEO_READABILITY_FILTER = "eq=contrast=1.04:saturation=1.10"

FPS = 24
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(FONTS_DIR, exist_ok=True)
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

APP_FONTS_DIR = "/app/fonts"

# POD TikTok captions use the bundled Bebas Neue font.
APP_FONT_FILE = os.path.join(APP_FONTS_DIR, "BebasNeue-Regular.ttf")
RUNTIME_FONT_FILE = os.path.join(FONTS_DIR, "BebasNeue-Regular.ttf")

# Use the same font for hook, punch, CTA and captions unless another impact font is added later.
APP_HUD_FONT_CANDIDATES = [
    os.path.join(APP_FONTS_DIR, "BebasNeue-Regular.ttf"),
]
RUNTIME_HUD_FONT_FILE = os.path.join(FONTS_DIR, "BebasNeue-Regular.ttf")

if os.path.exists(APP_FONT_FILE) and not os.path.exists(RUNTIME_FONT_FILE):
    shutil.copy(APP_FONT_FILE, RUNTIME_FONT_FILE)

if not os.path.exists(RUNTIME_HUD_FONT_FILE):
    for candidate in APP_HUD_FONT_CANDIDATES:
        if os.path.exists(candidate):
            shutil.copy(candidate, RUNTIME_HUD_FONT_FILE)
            break


def get_hud_font_file() -> str:
    """Returns the impact font for hook/punch/CTA, with a safe fallback."""
    if os.path.exists(RUNTIME_HUD_FONT_FILE):
        return RUNTIME_HUD_FONT_FILE
    return RUNTIME_FONT_FILE

app.mount("/video", StaticFiles(directory=VIDEO_DIR), name="video")

ASS_WHITE = r"\c&HFFFFFF&"  # LBN white, ASS BGR
ASS_GOLD = r"\c&H5AC1E6&"   # LBN warm gold, ASS BGR (#E6C15A)

IMPACT_WORDS = {
    "MEMORIA",
    "RECUERDO",
    "RECUERDOS",
    "RENTA",
    "ALQUILER",
    "CIUDAD",
    "SISTEMA",
    "FUTURO",
    "HUMANO",
    "HUMANA",
    "HUMANOS",
    "REAL",
    "SINTÉTICO",
    "SINTETICO",
    "ILEGAL",
    "PROHIBIDO",
    "VENDIÓ",
    "VENDIO",
    "VENDIDA",
    "VENDER",
    "COMPRÓ",
    "COMPRO",
    "BORRÓ",
    "BORRO",
    "PERDIÓ",
    "PERDIO",
    "OLVIDÓ",
    "OLVIDO",
    "AMOR",
    "MADRE",
    "PADRE",
    "FAMILIA",
    "CUERPO",
    "ROSTRO",
    "IDENTIDAD",
    "VIDA",
    "MUERTE",
    "EMOCIÓN",
    "EMOCION",
    "MENTIRA",
    "VERDAD",
    "POBRE",
    "LUJO",
    "PISO",
    "DISTRITO",
    "ARCHIVO",
    "CRÉDITO",
    "CREDITO",
    "NOVA",
}


def normalize_token(value: str) -> str:
    text = str(value or "").upper()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[^A-ZÑ]+", "", text)
    return text


def escape_ffmpeg_path(path: str) -> str:
    return (
        path.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
        .replace("[", r"\[")
        .replace("]", r"\]")
    )


def escape_drawtext_value(value: str) -> str:
    if not value:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("'", "’")
        .replace("%", "\\%")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def clean_display_text(value: str, max_words: int = 5) -> str:
    text = str(value or "").strip()
    text = text.replace("“", "").replace("”", "").replace('"', "")
    text = re.sub(r"\s+", " ", text)
    text = text.upper()

    words = text.split()
    if len(words) > max_words:
        words = words[:max_words]

    return " ".join(words).strip()


def split_headline(text: str) -> tuple[str, str]:
    words = clean_display_text(text, max_words=5).split()

    if not words:
        return "NO ERA", "DINERO"

    if len(words) == 1:
        return "", words[0]

    if len(words) == 2:
        return words[0], words[1]

    if len(words) == 3:
        return " ".join(words[:2]), words[2]

    top = " ".join(words[:-1])
    gold = words[-1]
    return top, gold


def build_hook_impact_lines(text: str) -> list[str]:
    """
    Hook HUD layout: always 3 vertical impact hits.

    Target structure:
    - line 1: max 1 word
    - line 2: 1-2 words
    - line 3: max 1 word

    The input should be 3-4 words. If it arrives shorter, a compact
    system-style opener is added to preserve the 3-hit retention rhythm.
    """
    words = clean_display_text(text, max_words=4).split()

    if not words:
        return ["NO", "ERA", "DINERO"]

    if len(words) == 1:
        return ["NO", words[0], "HOY"]

    if len(words) == 2:
        return ["LA", words[0], words[1]]

    if len(words) == 3:
        return [words[0], words[1], words[2]]

    return [words[0], " ".join(words[1:-1]), words[-1]]

def split_truth_punch_lines(text: str) -> list[str]:
    words = clean_display_text(text, max_words=4).split()

    if not words:
        return ["NO ERA", "DINERO"]

    if len(words) == 1:
        return [words[0]]

    if len(words) == 2:
        return [words[0], words[1]]

    if len(words) == 3:
        return [words[0], " ".join(words[1:])]

    return [" ".join(words[:2]), " ".join(words[2:])]



def split_cta_phrase_lines(text: str) -> list[str]:
    """
    Backward-compatible wrapper for legacy callers.

    CTA text is no longer restricted to 3-4 words. Hook cards and truth
    punch overlays stay compact, but the final CTA card is allowed to carry
    a full polarizing question or dilemma.
    """
    return split_cta_visual_text_into_lines(text, max_lines=3, max_chars=18)


def adjust_font_size_for_text(text: str, base_size: int, min_size: int = 72) -> int:
    plain = str(text or "")
    plain = plain.replace("\\,", ",")
    plain = plain.replace("\\:", ":")
    plain = re.sub(r"\s+", " ", plain).strip()
    char_count = len(plain)

    if char_count <= 8:
        scale = 1.00
    elif char_count <= 11:
        scale = 0.92
    elif char_count <= 14:
        scale = 0.82
    elif char_count <= 18:
        scale = 0.70
    elif char_count <= 22:
        scale = 0.60
    else:
        scale = 0.52

    return max(min_size, int(round(base_size * scale)))




def adjust_hud_font_size_for_width(
    text: str,
    base_size: int,
    min_size: int = 44,
    max_width: int = 600,
    char_width_ratio: float = 0.68,
) -> int:
    """
    Conservative font scaler for Archivo Black impact text.

    FFmpeg drawtext does not auto-wrap. This prevents hook/punch/CTA lines
    from touching or crossing the 720px vertical safe-area margins.
    """
    plain = str(text or "")
    plain = plain.replace("\\,", ",").replace("\\:", ":")
    plain = re.sub(r"\s+", " ", plain).strip()
    char_count = max(1, len(plain))

    size_by_content = adjust_font_size_for_text(plain, base_size, min_size=min_size)
    size_by_width = int(max_width / (char_count * char_width_ratio))
    return max(min_size, min(size_by_content, size_by_width, base_size))


def format_reference_stamp(value: str) -> str:
    """
    Converts long world metadata into a short, safe stamp.

    Example:
    NOVA LOS ANGELES · 2184 | LOVE CONTRACT DISTRICT
    -> NOVA LOS ANGELES · 2184
    """
    text = str(value or "").strip()
    if not text:
        return ""

    text = text.replace("—", "·").replace("–", "·")
    text = re.sub(r"\s+", " ", text)
    text = text.split("|")[0].strip()

    year_match = re.search(r"\b(20\d{2}|21\d{2}|22\d{2}|23\d{2})\b", text)
    if year_match:
        year = year_match.group(1)
        location = text[:year_match.start()].strip(" ·-:,/")
        location = re.sub(r"\bYEAR\b", "", location, flags=re.IGNORECASE).strip(" ·-:,/")
        if location:
            text = f"{location} · {year}"
        else:
            text = year

    text = text.upper()
    text = re.sub(r"\s*[·\-:]\s*", " · ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > 34:
        text = text[:34].rstrip(" ·-:,/")

    return text

def clean_cta_display_text(value: str, max_chars: int = 76) -> str:
    """
    Cleans CTA text for the final comment card.

    Unlike clean_display_text(), this function does NOT cap by word count.
    It preserves useful CTA punctuation such as ':', '/', and '?'.
    """
    text = str(value or "").strip()
    text = text.replace("“", "").replace("”", "").replace('"', "")
    text = text.replace("¿", "").replace("¡", "")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text)
    text = text.upper().strip()

    # Normalize common Spanish CTA verbs to English only for visual rendering.
    replacements = {
        r"^ELIGE\b": "CHOOSE",
        r"^DECIDE\b": "CHOOSE",
        r"^VOTA\b": "VOTE",
        r"^COMENTA\b": "COMMENT",
        r"^LO HARIAS\?": "WOULD YOU DO IT?",
        r"^LO HARÍAS\?": "WOULD YOU DO IT?",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"\s*:\s*", ": ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars].rstrip(" -:/")

    return text


def wrap_words_balanced(text: str, max_lines: int = 2, max_chars: int = 18) -> list[str]:
    text = clean_cta_display_text(text, max_chars=76)
    words = text.split()

    if not words:
        return []

    if len(text) <= max_chars:
        return [text]

    lines: list[str] = []
    current: list[str] = []

    for word in words:
        candidate = " ".join(current + [word]).strip()
        if current and len(candidate) > max_chars and len(lines) < max_lines - 1:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)

    if current:
        lines.append(" ".join(current))

    if len(lines) > max_lines:
        overflow = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [overflow]

    return [x.strip() for x in lines if x.strip()]


def normalize_cta_label(label: str) -> str:
    label = clean_cta_display_text(label, max_chars=32)
    label = label.rstrip(":").strip()

    translations = {
        "ELIGE": "CHOOSE",
        "DECIDE": "CHOOSE",
        "VOTA": "VOTE",
        "COMENTA": "COMMENT",
    }
    return translations.get(label, label)


def split_cta_visual_text_into_lines(text: str, max_lines: int = 4, max_chars: int = 18) -> list[str]:
    """
    Converts a long CTA into 2-4 short visual lines that fit inside the final HUD.

    Examples:
    - CHOOSE: ONE MORE MINUTE / CLEAN GOODBYE
      -> CHOOSE / ONE MORE MINUTE / CLEAN GOODBYE
    - WOULD YOU PAY WITH TEARS? YES / NO
      -> WOULD YOU PAY / WITH TEARS? / YES / NO
    - WHAT WOULD YOU CHOOSE: SPOUSE OR CHILD?
      -> WHAT WOULD YOU / CHOOSE? / SPOUSE OR CHILD?
    """
    full = clean_cta_display_text(text, max_chars=76)

    if not full:
        return ["YES / NO"]

    # Format: LABEL: OPTION A / OPTION B
    if ":" in full:
        raw_label, raw_body = full.split(":", 1)
        label = normalize_cta_label(raw_label)
        body = clean_cta_display_text(raw_body, max_chars=58)

        if label.startswith("WHAT WOULD YOU CHOOSE"):
            label_lines = ["WHAT WOULD YOU", "CHOOSE?"]
        elif label.startswith("WHO IS RIGHT"):
            label_lines = ["WHO IS RIGHT?"]
        elif label.startswith("WHO WAS RIGHT"):
            label_lines = ["WHO WAS RIGHT?"]
        elif label.startswith("WHO OWNS"):
            label_lines = wrap_words_balanced(label + "?", max_lines=2, max_chars=max_chars)
        else:
            label_lines = wrap_words_balanced(label, max_lines=2, max_chars=max_chars)

        body = body.rstrip("?").strip() if label.startswith("WHAT WOULD YOU CHOOSE") else body

        if "/" in body:
            options = [clean_cta_display_text(x, max_chars=26).rstrip("?").strip() for x in body.split("/")]
            options = [x for x in options if x]
            lines = label_lines + options[:2]
        else:
            lines = label_lines + wrap_words_balanced(body, max_lines=max_lines - len(label_lines), max_chars=max_chars)

        return lines[:max_lines]

    # Format: QUESTION? YES / NO
    if "YES / NO" in full:
        question = full.replace("YES / NO", "").strip()
        question = question if question.endswith("?") else question.rstrip(" ?") + "?"
        question_lines = wrap_words_balanced(question, max_lines=max_lines - 1, max_chars=max_chars)
        return (question_lines + ["YES / NO"])[:max_lines]

    # Format: SHORT QUESTION
    if "?" in full:
        return wrap_words_balanced(full, max_lines=max_lines, max_chars=max_chars)

    # Format: OPTION A / OPTION B
    if "/" in full:
        options = [clean_cta_display_text(x, max_chars=26).strip() for x in full.split("/")]
        options = [x for x in options if x]
        return options[:max_lines]

    return wrap_words_balanced(full, max_lines=max_lines, max_chars=max_chars)



def extract_quoted_cta(call_to_action: str, hook: str = "", guion: str = "") -> str:
    text = str(call_to_action or "")
    context = f"{call_to_action} {hook} {guion}".lower()

    match = re.search(r"[“\"]([^”\"]{2,80})[”\"]", text)
    if match:
        phrase = clean_cta_display_text(match.group(1), max_chars=48)
        if phrase:
            return phrase

    if "memory" in context or "memoria" in context or "recuerdo" in context:
        return "MEMORY / SURVIVAL"

    if "rent" in context or "renta" in context or "house" in context or "home" in context:
        return "MEMORY / HOME"

    if "love" in context or "amor" in context:
        return "LOVE / SAFETY"

    if "body" in context or "cuerpo" in context or "face" in context or "identity" in context:
        return "BODY / IDENTITY"

    if "child" in context or "son" in context or "daughter" in context or "famil" in context:
        return "FAMILY / LAW"

    if "sky" in context or "air" in context:
        return "AIR / SKY"

    return "YES / NO"


def extract_cta_visual_parts(call_to_action: str, hook: str = "", guion: str = "") -> tuple[str, str]:
    """
    Returns a readable CTA label and phrase for logs/backward compatibility.

    The renderer itself uses build_cta_visual_lines() so long CTAs can fit in
    multiple lines. This function no longer truncates the CTA to 3-4 words.
    """
    text = clean_cta_display_text(call_to_action, max_chars=76)

    if not text:
        return "CHOOSE", extract_quoted_cta(call_to_action, hook=hook, guion=guion)

    if ":" in text:
        label, body = text.split(":", 1)
        label = normalize_cta_label(label) or "CHOOSE"
        body = clean_cta_display_text(body, max_chars=58)
        if body:
            return label, body

    if "YES / NO" in text:
        question = text.replace("YES / NO", "").strip()
        question = question if question else "WOULD YOU DO IT?"
        return question, "YES / NO"

    if "/" in text:
        return "CHOOSE", text

    if "?" in text:
        return text, ""

    return "COMMENT", text


def build_cta_visual_lines(call_to_action: str, hook: str = "", guion: str = "") -> list[str]:
    text = clean_cta_display_text(call_to_action, max_chars=76)

    if not text:
        text = extract_quoted_cta(call_to_action, hook=hook, guion=guion)

    lines = split_cta_visual_text_into_lines(text, max_lines=4, max_chars=18)

    if not lines:
        lines = split_cta_visual_text_into_lines(
            extract_quoted_cta(call_to_action, hook=hook, guion=guion),
            max_lines=4,
            max_chars=18,
        )

    # Final safety: avoid a single very long line. This is rare but prevents
    # drawtext overflow on platform-specific CTAs.
    safe_lines: list[str] = []
    for line in lines:
        if len(line) > 22:
            safe_lines.extend(wrap_words_balanced(line, max_lines=2, max_chars=18))
        else:
            safe_lines.append(line)

    return safe_lines[:4]


def extract_truth_punch_text(guion: str) -> str:
    text = str(guion or "").strip().lower()

    if "memoria" in text or "recuerdo" in text:
        return "ERA MEMORIA"

    if "renta" in text or "alquiler" in text or "casa" in text:
        return "NO ERA DINERO"

    if "ciudad" in text or "sistema" in text:
        return "LA CIUDAD COBRÓ"

    if "amor" in text or "pareja" in text:
        return "AMOR SINTÉTICO"

    if "humano" in text or "real" in text:
        return "SEGUÍA SIENDO HUMANO"

    if "ilegal" in text or "prohibido" in text:
        return "ERA ILEGAL"

    if "madre" in text or "padre" in text or "familia" in text:
        return "PERDIÓ SU ORIGEN"

    if "cuerpo" in text or "rostro" in text or "identidad" in text:
        return "CAMBIÓ SU IDENTIDAD"

    if "pobre" in text or "lujo" in text:
        return "LA CIUDAD DIVIDIÓ"

    return "NO ERA DINERO"


def compute_truth_punch_window(voice_duration: float) -> tuple[float, float] | None:
    """
    Computes the mid-video truth punch window.

    This stays centralized so the overlay and subtitles agree on the same
    timing. Subtitles are suppressed during this window to keep the visual
    punch readable.
    """
    if voice_duration < 18:
        return None

    start_time = min(max(float(voice_duration) * 0.48, 11.5), max(12.0, float(voice_duration) - 8.0))
    end_time = min(float(voice_duration) - 3.0, start_time + TRUTH_PUNCH_DURATION)

    if end_time <= start_time:
        return None

    return round(start_time, 3), round(end_time, 3)


def get_audio_duration(audio_path: str) -> float:
    probes = [
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
    ]

    for cmd in probes:
        result = subprocess.run(cmd, capture_output=True, text=True)
        raw = (result.stdout or "").strip()
        if raw:
            for line in raw.splitlines():
                try:
                    value = float(line.strip())
                    if value > 0.2:
                        return value
                except Exception:
                    pass

    return 8.0


def download_file(url: str, path: str) -> str:
    urllib.request.urlretrieve(url, path)
    return path


def preferred_scene_weights(clip_count: int) -> list[float]:
    """
    Defines the visual pacing preference for ai_video_clean_N.

    The first scene stays shorter to protect the hook, but the remaining
    duration is distributed across all later scenes instead of pushing the
    entire surplus into scene 5.
    """
    if clip_count <= 0:
        return []

    presets = {
        1: [1.00],
        2: [0.38, 0.62],
        3: [0.18, 0.40, 0.42],
        4: [0.14, 0.26, 0.30, 0.30],
        5: [0.10, 0.19, 0.23, 0.24, 0.24],
    }

    if clip_count in presets:
        return presets[clip_count]

    return [1.0 / clip_count] * clip_count


def compute_scene_durations(
    total_duration: float,
    clip_count: int,
    clip_durations: list[float] | None = None
) -> list[float]:
    """
    Computes scene durations for the background video.

    Important behavior:
    - final_duration still includes END_TAIL_DURATION, so the video keeps a
      music-only ending after the voice.
    - scene 5 no longer absorbs the entire surplus.
    - when real clip durations are provided, no scene is assigned more than
      its available moving-video duration, except for a tiny technical pad.
    - if there is not enough visual material to cover the requested duration,
      the render fails instead of creating a long frozen ending.
    """
    if clip_count <= 0:
        return []

    target_total = max(MIN_SCENE_DURATION, float(total_duration or 0.0))

    if clip_count == 1:
        if clip_durations:
            capacity = max(MIN_SCENE_DURATION, float(clip_durations[0]))
            if target_total > capacity + MAX_CLONE_PAD_PER_SCENE:
                raise RuntimeError(
                    "Insufficient visual material: one clip is shorter than "
                    f"target duration ({capacity:.2f}s available vs {target_total:.2f}s needed)."
                )
        return [target_total]

    weights = preferred_scene_weights(clip_count)

    if not clip_durations:
        raw = [target_total * weight for weight in weights]
        total_raw = sum(raw) or target_total
        return [max(MIN_SCENE_DURATION, x * target_total / total_raw) for x in raw]

    capacities = [max(MIN_SCENE_DURATION, float(x or 0.0)) for x in clip_durations[:clip_count]]

    if len(capacities) < clip_count:
        capacities.extend([MIN_SCENE_DURATION] * (clip_count - len(capacities)))

    total_capacity = sum(capacities)
    max_supported_total = total_capacity + (MAX_CLONE_PAD_PER_SCENE * clip_count)

    if target_total > max_supported_total:
        raise RuntimeError(
            "Insufficient visual material for requested final duration: "
            f"available={total_capacity:.2f}s, "
            f"target={target_total:.2f}s, "
            f"max_allowed_with_tiny_pad={max_supported_total:.2f}s. "
            "Regenerate longer clips or reduce narration duration."
        )

    target_real_total = min(target_total, total_capacity)
    durations = [0.0] * clip_count
    remaining_indices = set(range(clip_count))
    remaining_duration = target_real_total

    # Capped proportional allocation. Any duration that would exceed a clip's
    # real capacity is reassigned to other clips that still have available motion.
    while remaining_indices and remaining_duration > 0:
        remaining_weight = sum(weights[i] for i in remaining_indices) or len(remaining_indices)
        capped_this_round = False

        for idx in list(remaining_indices):
            desired = remaining_duration * (weights[idx] / remaining_weight)

            if desired >= capacities[idx]:
                durations[idx] = capacities[idx]
                remaining_duration -= capacities[idx]
                remaining_indices.remove(idx)
                capped_this_round = True

        if not capped_this_round:
            for idx in list(remaining_indices):
                durations[idx] = remaining_duration * (weights[idx] / remaining_weight)
            remaining_duration = 0.0
            break

    # If the requested final duration is microscopically longer than the sum of
    # the clips, distribute only a tiny pad. Long clone padding is never allowed.
    pad_needed = target_total - sum(durations)
    if pad_needed > 0.001:
        per_scene_pad = pad_needed / clip_count
        if per_scene_pad > MAX_CLONE_PAD_PER_SCENE:
            raise RuntimeError(
                "Computed visual padding exceeds safe clone threshold: "
                f"per_scene_pad={per_scene_pad:.2f}s, "
                f"max={MAX_CLONE_PAD_PER_SCENE:.2f}s."
            )
        durations = [duration + per_scene_pad for duration in durations]

    # Keep exact total after floating point operations by applying a small
    # adjustment to the final scene, still respecting the tiny-pad rule.
    diff = target_total - sum(durations)
    if abs(diff) > 0.001:
        adjusted_last = durations[-1] + diff
        max_last = capacities[-1] + MAX_CLONE_PAD_PER_SCENE
        if adjusted_last > max_last + 0.001:
            raise RuntimeError(
                "Final duration adjustment would require unsafe freeze padding "
                f"on last scene: requested={adjusted_last:.2f}s, max={max_last:.2f}s."
            )
        durations[-1] = max(MIN_SCENE_DURATION, adjusted_last)

    return [max(0.05, float(x)) for x in durations]

def scene_crop_expression(scene_index: int) -> tuple[str, str]:
    patterns = [
        ("(iw-ow)/2+16*sin(t*0.35)", "(ih-oh)/2-10*cos(t*0.25)"),
        ("(iw-ow)/2-18*sin(t*0.32)", "(ih-oh)/2+12*sin(t*0.25)"),
        ("(iw-ow)/2+14*sin(t*0.30)", "(ih-oh)/2+12*cos(t*0.24)"),
        ("(iw-ow)/2-16*cos(t*0.28)", "(ih-oh)/2-12*sin(t*0.24)"),
        ("(iw-ow)/2+10*sin(t*0.25)", "(ih-oh)/2+10*cos(t*0.22)"),
    ]
    return patterns[scene_index % len(patterns)]


def build_background_from_videos(
    clip_paths: list,
    output_path: str,
    total_duration: float,
    job_id: str
) -> None:
    n = len(clip_paths)
    if n == 0:
        raise RuntimeError("No clip paths received")

    scale_width = 820
    scale_height = 1458

    real_clip_durations = [max(MIN_SCENE_DURATION, float(get_audio_duration(path))) for path in clip_paths]
    scene_durations = compute_scene_durations(total_duration, n, real_clip_durations)

    print(
        f"[{job_id}] clean_scene_durations="
        f"{[round(x, 2) for x in scene_durations]}, "
        f"real_clip_durations={[round(x, 2) for x in real_clip_durations]}, "
        f"visual_capacity={sum(real_clip_durations):.2f}s, "
        f"target_duration={total_duration:.2f}s",
        flush=True,
    )

    inputs = []
    for clip_path in clip_paths:
        inputs.extend(["-i", clip_path])

    filter_parts = []

    for i, clip_path in enumerate(clip_paths):
        target_scene_duration = max(MIN_SCENE_DURATION, float(scene_durations[i]))
        real_clip_duration = max(MIN_SCENE_DURATION, float(real_clip_durations[i]))

        trim_duration = min(real_clip_duration, target_scene_duration)
        freeze_duration = max(0.0, target_scene_duration - real_clip_duration)

        # Clips from Kling already include generated motion.
        # Keep FFmpeg crop centered/static to avoid synthetic wobble or micro-jitter.
        crop_x = "(iw-ow)/2"
        crop_y = "(ih-oh)/2"

        contrast = "eq=contrast=1.04:saturation=1.02"
        if i == 0:
            contrast = "eq=contrast=1.08:saturation=1.04"

        chain = (
            f"[{i}:v]"
            f"scale={scale_width}:{scale_height}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:x='{crop_x}':y='{crop_y}',"
            f"{contrast},"
            f"setsar=1,"
            f"fps={FPS},"
            f"trim=duration={trim_duration:.2f},"
            f"setpts=PTS-STARTPTS"
        )

        if freeze_duration > MAX_CLONE_PAD_PER_SCENE + 0.001:
            raise RuntimeError(
                f"Unsafe freeze prevented on scene {i + 1}: "
                f"real={real_clip_duration:.2f}s, "
                f"target={target_scene_duration:.2f}s, "
                f"freeze={freeze_duration:.2f}s, "
                f"max_allowed={MAX_CLONE_PAD_PER_SCENE:.2f}s"
            )

        if freeze_duration > 0.02:
            chain += f",tpad=stop_mode=clone:stop_duration={freeze_duration:.2f}"

        chain += f",format=yuv420p[v{i}]"

        filter_parts.append(chain)

        print(
            f"[{job_id}] scene_{i + 1}: real={real_clip_duration:.2f}s, "
            f"target={target_scene_duration:.2f}s, "
            f"trim={trim_duration:.2f}s, "
            f"freeze={freeze_duration:.2f}s",
            flush=True,
        )

    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[outv]")

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-t", f"{total_duration:.2f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-movflags", "+faststart",
        output_path
    ]

    print(f"[{job_id}] build_background_from_videos cmd: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[{job_id}] build_background_from_videos stderr: {result.stderr}", flush=True)
        raise RuntimeError(f"build_background_from_videos failed: {result.stderr}")

    if not os.path.exists(output_path):
        raise RuntimeError("output background not created")


def build_background_from_images(
    image_paths: list,
    output_path: str,
    total_duration: float,
    job_id: str
) -> None:
    n = len(image_paths)
    if n == 0:
        raise RuntimeError("No image paths received")

    clip_duration = total_duration / n

    inputs = []
    for img_path in image_paths:
        inputs.extend(["-i", img_path])

    filter_parts = []

    for i in range(n):
        frames = max(1, int(round(clip_duration * FPS)))

        filter_parts.append(
            f"[{i}:v]"
            f"scale=800:1422:force_original_aspect_ratio=increase,"
            f"crop=800:1422,"
            f"setsar=1,"
            f"zoompan="
            f"z='1+0.11*on/{frames}':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={frames}:"
            f"s={OUTPUT_WIDTH}x{OUTPUT_HEIGHT}:"
            f"fps={FPS},"
            f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=bicubic,"
            f"setpts=PTS-STARTPTS,"
            f"format=yuv420p"
            f"[v{i}]"
        )

    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[outv]")

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-t", f"{total_duration:.2f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "27",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-movflags", "+faststart",
        output_path
    ]

    print(f"[{job_id}] build_background_from_images cmd: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[{job_id}] build_background_from_images stderr: {result.stderr}", flush=True)
        raise RuntimeError(f"build_background_from_images failed: {result.stderr}")

    if not os.path.exists(output_path):
        raise RuntimeError("output background not created")


def build_reference_filter(referencia_biblica: str, start_time: float = REFERENCE_START_TIME) -> str:
    """
    Keeps the existing request field name for Make compatibility, but renders it
    as a short cinematic location/year stamp for this channel.
    """
    if not referencia_biblica or not referencia_biblica.strip():
        return ""

    if not os.path.exists(RUNTIME_FONT_FILE):
        return ""

    safe_font_path = escape_ffmpeg_path(RUNTIME_FONT_FILE)
    display_text = format_reference_stamp(referencia_biblica)
    safe_text = escape_drawtext_value(display_text)

    if not safe_text:
        return ""

    # Compact lower metadata stamp. It stays inside the mobile safe area and
    # avoids long case labels that previously ran outside the frame.
    return (
        f"drawbox="
        f"x=50:y=h-188:w=620:h=48:"
        f"color=black@0.26:t=fill:"
        f"enable=gte(t\\,{start_time:.2f}),"
        f"drawbox="
        f"x=50:y=h-188:w=4:h=48:"
        f"color=0x4BB8C8@0.68:t=fill:"
        f"enable=gte(t\\,{start_time:.2f}),"
        f"drawtext="
        f"fontfile={safe_font_path}:"
        f"text={safe_text}:"
        f"fontsize=24:"
        f"fontcolor=0xB8C7D9:"
        f"borderw=1:"
        f"bordercolor=black:"
        f"shadowx=1:"
        f"shadowy=1:"
        f"x=64:"
        f"y=h-176:"
        f"enable=gte(t\\,{start_time:.2f})"
    )


def add_pop_drawtext(
    filters: list,
    safe_font_path: str,
    text: str,
    final_size: int,
    fontcolor: str,
    center_y: int,
    start_time: float,
    end_time: float,
    borderw: int = 6,
    shadow: int = 3,
    overshoot_scale: float = 1.10,
    start_scale: float = 0.82,
    phase1_duration: float = 0.06,
    phase2_duration: float = 0.14,
):
    if not text:
        return

    phase1_end = min(end_time, start_time + phase1_duration)
    phase2_end = min(end_time, start_time + phase2_duration)

    phases = [
        (start_time, phase1_end, max(1, int(round(final_size * start_scale)))),
        (phase1_end, phase2_end, max(1, int(round(final_size * overshoot_scale)))),
        (phase2_end, end_time, final_size),
    ]

    for phase_start, phase_end, size in phases:
        if phase_end <= phase_start:
            continue

        enable = f"between(t\\,{phase_start:.2f}\\,{phase_end:.2f})"

        filters.append(
            f"drawtext="
            f"fontfile={safe_font_path}:"
            f"text={text}:"
            f"fontsize={size}:"
            f"fontcolor={fontcolor}:"
            f"borderw={borderw}:"
            f"bordercolor=black:"
            f"shadowx={shadow}:"
            f"shadowy={shadow}:"
            f"x=(w-text_w)/2:"
            f"y={center_y}-text_h/2:"
            f"enable={enable}"
        )


def build_hook_card_filters(hook_visual_text: str) -> list:
    if not os.path.exists(RUNTIME_FONT_FILE):
        return []

    safe_font_path = escape_ffmpeg_path(get_hud_font_file())

    lines = build_hook_impact_lines(hook_visual_text)
    safe_lines = [escape_drawtext_value(x) for x in lines if x and x.strip()]

    if len(safe_lines) < 3:
        safe_lines = ["NO", "ERA", "DINERO"]

    first, second, third = safe_lines[:3]

    enable_flash = f"between(t\\,0.00\\,{HOOK_CARD_START:.2f})"
    enable_card = f"between(t\\,{HOOK_CARD_START:.2f}\\,{HOOK_CARD_END:.2f})"

    panel_x = 48
    panel_y = 260
    panel_w = 624
    panel_h = 610
    panel_bottom = panel_y + panel_h

    filters = [
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.30:t=fill:enable={enable_flash}",
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.18:t=fill:enable={enable_card}",
        f"drawbox=x={panel_x}:y={panel_y}:w={panel_w}:h={panel_h}:color=black@0.44:t=fill:enable={enable_card}",
        f"drawbox=x={panel_x}:y={panel_y}:w=5:h={panel_h}:color=0x00E5FF@0.78:t=fill:enable={enable_card}",
        f"drawbox=x={panel_x}:y={panel_y}:w={panel_w}:h=2:color=0x00E5FF@0.32:t=fill:enable={enable_card}",
        f"drawbox=x={panel_x}:y={panel_bottom - 2}:w={panel_w}:h=2:color=0x00E5FF@0.26:t=fill:enable={enable_card}",
    ]

    first_size = adjust_hud_font_size_for_width(first, 80, min_size=54, max_width=560)
    second_size = adjust_hud_font_size_for_width(second, 112, min_size=62, max_width=580)
    third_size = adjust_hud_font_size_for_width(third, 138, min_size=74, max_width=590)

    add_pop_drawtext(
        filters=filters,
        safe_font_path=safe_font_path,
        text=first,
        final_size=first_size,
        fontcolor="0xF4F7FF",
        center_y=410,
        start_time=HOOK_WORD_1_START,
        end_time=HOOK_CARD_END,
        borderw=4,
        shadow=2,
        overshoot_scale=1.04,
        start_scale=0.84,
    )

    add_pop_drawtext(
        filters=filters,
        safe_font_path=safe_font_path,
        text=second,
        final_size=second_size,
        fontcolor="0xF4F7FF",
        center_y=560,
        start_time=HOOK_WORD_2_START,
        end_time=HOOK_CARD_END,
        borderw=5,
        shadow=2,
        overshoot_scale=1.05,
        start_scale=0.82,
    )

    add_pop_drawtext(
        filters=filters,
        safe_font_path=safe_font_path,
        text=third,
        final_size=third_size,
        fontcolor="0x00E5FF",
        center_y=715,
        start_time=HOOK_WORD_3_START,
        end_time=HOOK_CARD_END,
        borderw=6,
        shadow=3,
        overshoot_scale=1.06,
        start_scale=0.80,
    )

    return filters


def build_truth_punch_filters(
    guion: str,
    voice_duration: float,
    truth_punch_text: str = ""
) -> list:
    if not os.path.exists(RUNTIME_FONT_FILE):
        return []

    if voice_duration < 18:
        return []

    safe_font_path = escape_ffmpeg_path(get_hud_font_file())

    incoming_truth_punch = clean_display_text(truth_punch_text, max_words=4)
    resolved_truth_punch = incoming_truth_punch or extract_truth_punch_text(guion)

    punch_lines = split_truth_punch_lines(resolved_truth_punch)
    safe_lines = [escape_drawtext_value(x) for x in punch_lines if x and x.strip()]

    if not safe_lines:
        return []

    safe_lines = safe_lines[:2]

    truth_window = compute_truth_punch_window(voice_duration)
    if not truth_window:
        return []

    start_time, end_time = truth_window

    enable = f"between(t\\,{start_time:.2f}\\,{end_time:.2f})"

    panel_x = 58
    panel_y = 492
    panel_w = 604
    panel_h = 220
    panel_bottom = panel_y + panel_h

    filters = [
        f"drawbox=x={panel_x}:y={panel_y}:w={panel_w}:h={panel_h}:color=black@0.46:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_y}:w=5:h={panel_h}:color=0x00E5FF@0.78:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_y}:w={panel_w}:h=2:color=0x00E5FF@0.30:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_bottom - 2}:w={panel_w}:h=2:color=0x00E5FF@0.25:t=fill:enable={enable}",
    ]

    first_start = start_time + 0.08
    second_start = start_time + 0.34

    if len(safe_lines) == 1:
        line = safe_lines[0]
        size = adjust_hud_font_size_for_width(line, 88, min_size=56, max_width=560)
        add_pop_drawtext(
            filters=filters,
            safe_font_path=safe_font_path,
            text=line,
            final_size=size,
            fontcolor="0x00E5FF",
            center_y=602,
            start_time=first_start,
            end_time=end_time,
            borderw=5,
            shadow=2,
            overshoot_scale=1.04,
            start_scale=0.86,
            phase1_duration=0.12,
            phase2_duration=0.28,
        )
        return filters

    first, second = safe_lines[0], safe_lines[1]
    first_size = adjust_hud_font_size_for_width(first, 62, min_size=46, max_width=540)
    second_size = adjust_hud_font_size_for_width(second, 88, min_size=54, max_width=560)

    add_pop_drawtext(
        filters=filters,
        safe_font_path=safe_font_path,
        text=first,
        final_size=first_size,
        fontcolor="0xF4F7FF",
        center_y=565,
        start_time=first_start,
        end_time=end_time,
        borderw=4,
        shadow=2,
        overshoot_scale=1.03,
        start_scale=0.88,
        phase1_duration=0.12,
        phase2_duration=0.28,
    )

    add_pop_drawtext(
        filters=filters,
        safe_font_path=safe_font_path,
        text=second,
        final_size=second_size,
        fontcolor="0x00E5FF",
        center_y=650,
        start_time=second_start,
        end_time=end_time,
        borderw=5,
        shadow=2,
        overshoot_scale=1.04,
        start_scale=0.86,
        phase1_duration=0.12,
        phase2_duration=0.28,
    )

    return filters



def build_cta_card_filters(
    call_to_action: str,
    hook: str,
    guion: str,
    cta_start_time: float,
    final_duration: float
) -> list:
    if not os.path.exists(RUNTIME_FONT_FILE):
        return []

    if final_duration < 8:
        return []

    safe_font_path = escape_ffmpeg_path(get_hud_font_file())

    visual_lines = build_cta_visual_lines(call_to_action, hook=hook, guion=guion)
    safe_lines = [escape_drawtext_value(x) for x in visual_lines if x and x.strip()]

    if not safe_lines:
        return []

    safe_lines = safe_lines[:4]

    start_time = cta_start_time
    end_time = final_duration - 0.05

    if end_time <= start_time + 0.4:
        return []

    enable = f"between(t\\,{start_time:.2f}\\,{end_time:.2f})"

    line_count = len(safe_lines)

    # Dedicated long-CTA layout. It is intentionally centered vertically,
    # not top and not bottom. It stays inside safe margins while giving
    # long questions/options enough room to breathe.
    panel_x = 50
    panel_y = 460
    panel_w = 620
    panel_h = 360
    panel_bottom = panel_y + panel_h

    filters = [
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.14:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_y}:w={panel_w}:h={panel_h}:color=black@0.58:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_y}:w=5:h={panel_h}:color=0x00E5FF@0.82:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_y}:w={panel_w}:h=2:color=0x00E5FF@0.34:t=fill:enable={enable}",
        f"drawbox=x={panel_x}:y={panel_bottom - 2}:w={panel_w}:h=2:color=0x00E5FF@0.28:t=fill:enable={enable}",
    ]

    if line_count == 1:
        centers = [640]
        base_sizes = [82]
        min_sizes = [44]
        colors = ["0x00E5FF"]
    elif line_count == 2:
        centers = [585, 695]
        base_sizes = [58, 76]
        min_sizes = [42, 44]
        colors = ["0xF4F7FF", "0x00E5FF"]
    elif line_count == 3:
        centers = [540, 640, 740]
        base_sizes = [52, 70, 70]
        min_sizes = [38, 42, 42]
        colors = ["0xF4F7FF", "0x00E5FF", "0x00E5FF"]
    else:
        centers = [500, 595, 685, 780]
        base_sizes = [46, 62, 62, 62]
        min_sizes = [36, 38, 38, 38]
        colors = ["0xF4F7FF", "0x00E5FF", "0x00E5FF", "0x00E5FF"]

    for index, line in enumerate(safe_lines):
        line_start = start_time + 0.06 + (index * 0.18)
        line_size = adjust_hud_font_size_for_width(
            line,
            base_size=base_sizes[index],
            min_size=min_sizes[index],
            max_width=560,
            char_width_ratio=0.72,
        )

        borderw = 3 if index == 0 else 4
        shadow = 2
        overshoot = 1.03 if index == 0 else 1.05
        start_scale = 0.86 if index == 0 else 0.82

        add_pop_drawtext(
            filters=filters,
            safe_font_path=safe_font_path,
            text=line,
            final_size=line_size,
            fontcolor=colors[index],
            center_y=centers[index],
            start_time=line_start,
            end_time=end_time,
            borderw=borderw,
            shadow=shadow,
            overshoot_scale=overshoot,
            start_scale=start_scale,
            phase1_duration=0.10,
            phase2_duration=0.22,
        )

    return filters


def validate_cta_for_render(call_to_action: str) -> None:
    text = str(call_to_action or "").strip()

    if not text:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "call_to_action llegó vacío. Se recomienda enviar el CTA visual final fijo para POD.",
                "expected_format": "SÍGUENOS PARA MÁS PALABRA DE DIOS CLARA",
            }
        )

    if len(text.split()) < 2:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "call_to_action es demasiado corto para renderizar CTA visual confiable.",
                "call_to_action": text,
                "expected_format": "SÍGUENOS PARA MÁS PALABRA DE DIOS CLARA",
            }
        )


def seconds_to_ass_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def escape_ass_text(text: str) -> str:
    return (
        str(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def speed_up_alignment(alignment: dict, speed: float) -> dict:
    return {
        "characters": alignment.get("characters", []),
        "character_start_times_seconds": [
            float(x) / speed for x in alignment.get("character_start_times_seconds", [])
        ],
        "character_end_times_seconds": [
            float(x) / speed for x in alignment.get("character_end_times_seconds", [])
        ],
    }


def build_words_from_alignment(alignment: dict) -> list:
    characters = alignment.get("characters", [])
    starts = alignment.get("character_start_times_seconds", [])
    ends = alignment.get("character_end_times_seconds", [])

    if not characters or not starts or not ends:
        return []

    words = []
    current_chars = []
    current_start = None
    current_end = None

    for ch, st, en in zip(characters, starts, ends):
        try:
            st = float(st)
            en = float(en)
        except Exception:
            continue

        if str(ch).isspace():
            if current_chars:
                word = "".join(current_chars).strip()
                if word:
                    words.append({
                        "word": word,
                        "start": float(current_start),
                        "end": float(current_end),
                    })
                current_chars = []
                current_start = None
                current_end = None
            continue

        if current_start is None:
            current_start = st

        current_chars.append(str(ch))
        current_end = en

    if current_chars:
        word = "".join(current_chars).strip()
        if word:
            words.append({
                "word": word,
                "start": float(current_start),
                "end": float(current_end),
            })

    return words


def tokenize_for_alignment_match(text: str) -> list[str]:
    """
    Converts text into normalized word tokens for matching against ElevenLabs alignment.
    Works with any CTA wording: Comenta, Sígueme, Escribe, Guarda, Ora, etc.
    """
    raw = str(text or "").strip()

    if not raw:
        return []

    raw = raw.replace("“", " ").replace("”", " ").replace('"', " ")
    raw = "".join(
        c for c in unicodedata.normalize("NFD", raw)
        if unicodedata.category(c) != "Mn"
    )
    raw = raw.upper()
    raw = re.sub(r"[^A-ZÑ0-9\s]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    tokens = []
    for part in raw.split():
        token = normalize_token(part)
        if token:
            tokens.append(token)

    return tokens


def find_sequence_start_in_words(
    words: list,
    target_text: str,
    fallback_time: float,
    min_match_tokens: int = 4,
    search_after_ratio: float = 0.45
) -> float:
    """
    Finds the start time of target_text inside ElevenLabs word alignment.

    This is CTA-wording agnostic:
    - Comenta “...”
    - Sígueme si...
    - Escribe “...”
    - Guarda este video...
    - Ora conmigo...

    It searches the latter part of the narration to avoid matching earlier repeated words.
    If it cannot find a confident match, it falls back near the end of the voice.
    """
    if not words:
        return fallback_time

    target_tokens = tokenize_for_alignment_match(target_text)
    alignment_tokens = [normalize_token(item.get("word", "")) for item in words]

    indexed_tokens = [
        (idx, token)
        for idx, token in enumerate(alignment_tokens)
        if token
    ]

    if not target_tokens or not indexed_tokens:
        return fallback_time

    total_words = len(indexed_tokens)
    search_start_position = int(total_words * search_after_ratio)

    max_window = min(6, len(target_tokens))
    min_window = min(min_match_tokens, max_window)

    for window_size in range(max_window, max(2, min_window) - 1, -1):
        target_window = target_tokens[:window_size]

        for pos in range(search_start_position, total_words - window_size + 1):
            candidate = [indexed_tokens[pos + offset][1] for offset in range(window_size)]

            if candidate == target_window:
                original_word_index = indexed_tokens[pos][0]
                try:
                    return max(0.0, float(words[original_word_index].get("start", fallback_time)) - 0.05)
                except Exception:
                    return fallback_time

    quoted_match = re.search(r"[“\"]([^”\"]{2,80})[”\"]", str(target_text or ""))
    if quoted_match:
        quoted_tokens = tokenize_for_alignment_match(quoted_match.group(1))

        if quoted_tokens:
            max_window = min(4, len(quoted_tokens))
            for window_size in range(max_window, 1, -1):
                target_window = quoted_tokens[:window_size]

                for pos in range(search_start_position, total_words - window_size + 1):
                    candidate = [indexed_tokens[pos + offset][1] for offset in range(window_size)]

                    if candidate == target_window:
                        original_word_index = indexed_tokens[pos][0]
                        try:
                            return max(0.0, float(words[original_word_index].get("start", fallback_time)) - 0.35)
                        except Exception:
                            return fallback_time

    return fallback_time


def split_word_items_two_lines(word_items: list, max_line_chars: int = 18) -> list:
    if not word_items:
        return []

    words = [str(item["word"]) for item in word_items]
    if len(words) <= 1:
        return [word_items]

    best_split_index = None
    best_score = None

    for i in range(1, len(words)):
        line1 = " ".join(words[:i])
        line2 = " ".join(words[i:])

        if len(line1) > max_line_chars or len(line2) > max_line_chars:
            continue

        score = abs(len(line1) - len(line2))
        if best_score is None or score < best_score:
            best_score = score
            best_split_index = i

    if best_split_index is None:
        midpoint = len(words) // 2
        return [word_items[:midpoint], word_items[midpoint:]]

    return [word_items[:best_split_index], word_items[best_split_index:]]


def group_words_into_cues(words: list, max_words: int = 4, max_chars: int = 26) -> list:
    cues = []
    bucket = []

    def flush_bucket():
        nonlocal bucket
        if not bucket:
            return

        raw_text = " ".join(str(item["word"]) for item in bucket).strip()
        if raw_text:
            start_value = float(bucket[0]["start"])
            end_value = float(bucket[-1]["end"])

            cues.append({
                "text": raw_text.upper(),
                "start": start_value,
                "end": end_value,
                "words": [
                    {
                        "word": str(item["word"]).upper(),
                        "start": float(item["start"]),
                        "end": float(item["end"]),
                    }
                    for item in bucket
                ],
            })

        bucket = []

    for item in words:
        candidate_words = bucket + [item]
        candidate_text = " ".join(str(x["word"]) for x in candidate_words)

        punctuation_break = bool(re.search(r"[.!?,;:]$", str(item["word"])))
        too_many_words = len(candidate_words) > max_words
        too_many_chars = len(candidate_text) > max_chars

        if bucket and (too_many_words or too_many_chars):
            flush_bucket()

        bucket.append(item)

        if punctuation_break:
            flush_bucket()

    flush_bucket()

    for cue in cues:
        cue["start"] = float(cue["start"])
        cue["end"] = float(cue["end"])

        if cue["end"] - cue["start"] < 0.35:
            cue["end"] = cue["start"] + 0.35

    return cues


def build_line_groups(word_items: list, max_line_chars: int = 16) -> list:
    split_lines = split_word_items_two_lines(word_items, max_line_chars=max_line_chars)
    groups = []
    flat_index = 0

    for line_items in split_lines:
        group = []
        for item in line_items:
            group.append({
                "index": flat_index,
                "word": str(item["word"]).upper(),
                "start": float(item["start"]),
                "end": float(item["end"]),
            })
            flat_index += 1
        groups.append(group)

    return groups


def should_highlight_word(word: str) -> bool:
    normalized = normalize_token(word)
    if not normalized:
        return False

    normalized_impact = {normalize_token(x) for x in IMPACT_WORDS}
    return normalized in normalized_impact


def build_ass_dialogue_text(groups: list, active_index: int | None = None) -> str:
    line_texts = []

    for line in groups:
        parts = []
        for item in line:
            word_text = escape_ass_text(item["word"])
            is_active = active_index is not None and item["index"] == active_index
            is_impact = should_highlight_word(item["word"])

            if is_active or is_impact:
                parts.append(r"{" + ASS_GOLD + r"}" + word_text + r"{" + ASS_WHITE + r"}")
            else:
                parts.append(word_text)

        line_texts.append(" ".join(parts))

    prefix = r"{\an2\fs76\bord4\shad0\fscx100\fscy100\fsp0" + ASS_WHITE + r"}"
    return prefix + r"\N".join(line_texts)


def write_ass_subtitles(
    subtitles_path: str,
    cues: list,
    cta_start_time: float | None = None,
    truth_punch_window: tuple[float, float] | None = None,
):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Bebas Neue,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,0,2,80,80,285,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    with open(subtitles_path, "w", encoding="utf-8") as f:
        f.write(header)

        for cue in cues:
            cue_start = float(cue["start"])
            cue_end = float(cue["end"])

            # POD no-card mode: captions start immediately and continue through the end.
            # No hook cards, no CTA cards, no caption cutoff.

            if truth_punch_window is not None:
                truth_start, truth_end = truth_punch_window
                if cue_start < truth_end and cue_end > truth_start:
                    continue

            groups = build_line_groups(
                cue.get("words", []),
                max_line_chars=14
            )

            if not groups:
                continue

            flat_words = [item for line in groups for item in line]
            if not flat_words:
                continue

            segments = []
            cursor = cue_start
            eps = 0.01

            for item in flat_words:
                word_start = max(cue_start, float(item["start"]))
                word_end = min(cue_end, float(item["end"]))

                if cta_start_time is not None and word_start >= cta_start_time:
                    continue

                if cta_start_time is not None:
                    word_end = min(word_end, cta_start_time)

                if word_start > cursor + eps:
                    segments.append({
                        "start": cursor,
                        "end": word_start,
                        "active_index": None,
                    })

                if word_end > word_start + eps:
                    segments.append({
                        "start": word_start,
                        "end": word_end,
                        "active_index": item["index"],
                    })

                cursor = max(cursor, word_end)

            if cta_start_time is not None:
                cue_end = min(cue_end, cta_start_time)

            if cue_end > cursor + eps:
                segments.append({
                    "start": cursor,
                    "end": cue_end,
                    "active_index": None,
                })

            merged_segments = []
            for seg in segments:
                if seg["end"] <= seg["start"] + eps:
                    continue

                if (
                    merged_segments
                    and merged_segments[-1]["active_index"] == seg["active_index"]
                    and abs(merged_segments[-1]["end"] - seg["start"]) <= eps
                ):
                    merged_segments[-1]["end"] = seg["end"]
                else:
                    merged_segments.append(seg)

            for seg in merged_segments:
                start = seconds_to_ass_time(seg["start"])
                end = seconds_to_ass_time(seg["end"])
                text = build_ass_dialogue_text(
                    groups,
                    active_index=seg["active_index"],
                )
                f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")



def validate_video_filter_for_ffmpeg(filter_value: str) -> None:
    """
    Guardrail against FFmpeg drawtext parser failures.

    This renderer passes filtergraphs directly to subprocess, not through a shell.
    Therefore drawtext option values are emitted unquoted and fully escaped.
    The validator blocks known failure patterns before spending render time.
    """
    value = str(filter_value or "")

    bad_patterns = [
        "enable='between(",
        "enable='gte(",
        ":text='",
        ":fontfile='",
        "between(t,",
        "gte(t,",
    ]

    for pattern in bad_patterns:
        if pattern in value:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Unsafe FFmpeg filtergraph detected before render.",
                    "pattern": pattern,
                    "filter_excerpt": value[max(0, value.find(pattern) - 120): value.find(pattern) + 240],
                }
            )

@app.get("/")
def health():
    return {
        "status": "running",
        "font_exists": os.path.exists(RUNTIME_FONT_FILE),
        "font_path": RUNTIME_FONT_FILE,
        "hud_font_exists": os.path.exists(RUNTIME_HUD_FONT_FILE),
        "hud_font_path": get_hud_font_file(),
        "music_exists": os.path.exists(MUSIC_FILE),
        "music_path": MUSIC_FILE,
        "end_tail_duration": END_TAIL_DURATION,
        "scene_duration_mode": "pod_single_avatar_video_no_tail",
        "max_clone_pad_per_scene": MAX_CLONE_PAD_PER_SCENE,
        "hook_card_start": HOOK_CARD_START,
        "hook_card_end": HOOK_CARD_END,
        "hook_word_1_start": HOOK_WORD_1_START,
        "hook_word_2_start": HOOK_WORD_2_START,
        "hook_word_3_start": HOOK_WORD_3_START,
        "hook_card_mode": "disabled_no_cards",
        "truth_punch_mode": "disabled_for_pod",
        "cta_card_mode": "disabled_no_cards",
        "cta_detection_mode": "disabled_no_visual_cta",
        "reference_start_time": REFERENCE_START_TIME,
        "cta_card_duration": CTA_CARD_DURATION,
        "truth_punch_duration": TRUTH_PUNCH_DURATION,
        "music_required": False,
        "cta_card_required": False,
        "voice_starts_at": "0.00s",
        "sfx_enabled": False,
        "render_style": "pod_tiktok_avatar_captioned",
    }


class RenderRequest(BaseModel):
    numero_regla: str = ""
    hook: str = ""
    hook_visual_text: str = ""
    call_to_action: str = ""
    guion: str
    truth_punch_text: str = ""
    subtitles_mode: str = "dynamic"
    audio_base64: str
    normalized_alignment: dict

    referencia_biblica: str = ""

    video_url: str = ""
    video_url_1: str = ""
    video_url_2: str = ""
    video_url_3: str = ""
    video_url_4: str = ""
    video_url_5: str = ""

    image_url: str = ""
    image_url_2: str = ""
    image_url_3: str = ""
    image_url_4: str = ""
    image_url_5: str = ""


@app.post("/render")
async def render_video(data: RenderRequest):
    if not os.path.exists(RUNTIME_FONT_FILE):
        raise HTTPException(
            status_code=500,
            detail=f"La fuente no existe en runtime: {RUNTIME_FONT_FILE}"
        )

    # POD no-card mode: CTA is not rendered here. Keep field compatibility only.
    effective_call_to_action = (data.call_to_action or "").strip()

    job_id = str(uuid.uuid4())

    input_audio_path = os.path.join(AUDIO_DIR, f"{job_id}.mp3")
    voice_audio_path = os.path.join(AUDIO_DIR, f"{job_id}_voice.mp3")
    final_audio_path = os.path.join(AUDIO_DIR, f"{job_id}_final.mp3")
    subtitles_path = os.path.join(BASE_DIR, f"{job_id}.ass")
    video_path = os.path.join(VIDEO_DIR, f"{job_id}.mp4")

    try:
        audio_bytes = base64.b64decode(data.audio_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="audio_base64 inválido")

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="audio_base64 llegó vacío")

    with open(input_audio_path, "wb") as f:
        f.write(audio_bytes)

    # POD avatar video is generated from the same ElevenLabs audio. Do not speed it up or lipsync will drift.
    speed_factor = 1.0

    normalize_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", input_audio_path,
        "-vn",
        "-filter:a", f"atempo={speed_factor}",
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "192k",
        voice_audio_path
    ]

    normalize_result = subprocess.run(normalize_cmd, capture_output=True, text=True)

    if normalize_result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Error normalizando audio",
                "returncode": normalize_result.returncode,
                "stdout": normalize_result.stdout,
                "stderr": normalize_result.stderr,
            }
        )

    voice_duration = round(get_audio_duration(voice_audio_path), 3)
    final_duration = round(voice_duration + END_TAIL_DURATION, 3)

    # POD no-card mode: captions must continue until the final spoken word.
    # Do not compute a CTA cutoff, because there is no CTA card that should
    # suppress subtitles at the end.
    cta_start_time = None

    # POD render: music is optional. If /app/music/background.mp3 exists, mix it quietly.
    # If it does not exist, preserve the voice audio only.
    if os.path.exists(MUSIC_FILE):
        mix_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-stream_loop", "-1",
            "-i", MUSIC_FILE,
            "-i", voice_audio_path,
            "-filter_complex",
            (
                f"[0:a]volume=0.10,"
                f"atrim=0:{final_duration:.2f},"
                f"asetpts=PTS-STARTPTS,"
                f"afade=t=out:st={max(0.0, final_duration - 0.5):.2f}:d=0.5[bg];"
                f"[1:a]volume=1.0,"
                f"apad=pad_dur={END_TAIL_DURATION},"
                f"atrim=0:{final_duration:.2f},"
                f"asetpts=PTS-STARTPTS[voice];"
                f"[bg][voice]amix=inputs=2:duration=longest:dropout_transition=0,"
                f"atrim=0:{final_duration:.2f}[aout]"
            ),
            "-map", "[aout]",
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            final_audio_path
        ]

        mix_result = subprocess.run(mix_cmd, capture_output=True, text=True)

        if mix_result.returncode != 0 or not os.path.exists(final_audio_path):
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Error mezclando audio final",
                    "returncode": mix_result.returncode,
                    "stdout": mix_result.stdout,
                    "stderr": mix_result.stderr,
                }
            )
    else:
        shutil.copy(voice_audio_path, final_audio_path)

    final_audio_duration = round(get_audio_duration(final_audio_path), 3)

    if final_audio_duration < final_duration - 0.25:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "El audio final quedó más corto que el video. Se cancela para evitar cola en silencio.",
                "voice_duration": voice_duration,
                "final_duration": final_duration,
                "final_audio_duration": final_audio_duration,
                "music_used": os.path.exists(MUSIC_FILE),
            }
        )

    print(
        f"[{job_id}] voice_duration={voice_duration:.2f}s, "
        f"final_duration={final_duration:.2f}s, "
        f"final_audio_duration={final_audio_duration:.2f}s, "
        "cta_start_time=None (POD no-card mode; captions continue to end)",
        flush=True
    )

    adjusted_alignment = speed_up_alignment(data.normalized_alignment, speed_factor)
    words = build_words_from_alignment(adjusted_alignment)

    # Important: keep cta_start_time=None. In POD mode there is no final CTA
    # card, so subtitles must not be cut off near the last 2-3 seconds.
    cues = group_words_into_cues(words, max_words=3, max_chars=22)
    truth_punch_window = None
    write_ass_subtitles(
        subtitles_path,
        cues,
        cta_start_time=None,
        truth_punch_window=truth_punch_window,
    )

    safe_subtitles_path = escape_ffmpeg_path(subtitles_path)
    safe_fonts_dir = escape_ffmpeg_path(FONTS_DIR)

    # POD no-card mode: do not render reference stamps or any boxed metadata overlay.
    reference_filter = ""

    # POD no-card mode: no hook card, no truth-punch card, no CTA card.
    # The visual language is only the avatar + dynamic captions.
    hook_text = data.hook_visual_text or data.hook or ""
    incoming_truth_punch = ""
    resolved_truth_punch = ""
    cta_card_filters = []

    print(
        f"[{job_id}] POD NO-CARD MODE: "
        f"voice_duration={voice_duration:.2f}, "
        f"final_duration={final_duration:.2f}, "
        f"captions=dynamic, "
        f"cards=disabled",
        flush=True
    )

    def compose_video_filter(prefix_filter: str = "") -> str:
        parts = []

        if prefix_filter:
            parts.append(prefix_filter)

        if reference_filter:
            parts.append(reference_filter)

        parts.append(f"subtitles={safe_subtitles_path}:fontsdir={safe_fonts_dir}")

        # POD no-card mode: do not render hook cards, CTA cards, or truth-punch cards.

        return ",".join(parts)

    # Forward-compatible media logic:
    # - Current POD: send only video_url (single avatar video).
    # - Future POD multi-clip: send video_url_1..video_url_5. If video_url_1 exists,
    #   the renderer ignores video_url and uses the numbered clip set.
    numbered_video_urls = [
        data.video_url_1,
        data.video_url_2,
        data.video_url_3,
        data.video_url_4,
        data.video_url_5,
    ]
    has_numbered_clips = any(url and url.strip() for url in numbered_video_urls)
    candidate_video_urls = numbered_video_urls if has_numbered_clips else [data.video_url]

    video_urls = []
    for url in candidate_video_urls:
        if url and url.strip():
            video_urls.append(url.strip())

    image_urls = []
    for url in [
        data.image_url,
        data.image_url_2,
        data.image_url_3,
        data.image_url_4,
        data.image_url_5,
    ]:
        if url and url.strip():
            image_urls.append(url.strip())

    use_videos = len(video_urls) > 0
    use_images = (not use_videos) and len(image_urls) > 0
    render_mode = "black_background"
    media_count = 0

    if use_videos:
        try:
            clip_paths = []
            for i, url in enumerate(video_urls):
                clip_path = os.path.join(CLIPS_DIR, f"{job_id}_clip{i}.mp4")
                download_file(url, clip_path)
                clip_paths.append(clip_path)

            bg_video_path = os.path.join(CLIPS_DIR, f"{job_id}_bg.mp4")
            build_background_from_videos(clip_paths, bg_video_path, final_duration, job_id)

            overlay_filter = AI_VIDEO_READABILITY_FILTER
            video_filter = compose_video_filter(overlay_filter)
            render_mode = f"ai_video_clean_{len(clip_paths)}"
            media_count = len(clip_paths)

            ffmpeg_cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", bg_video_path,
                "-i", final_audio_path,
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-t", f"{final_duration:.2f}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "26",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "44100",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                video_path
            ]

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"build_background_from_videos fallo: {str(e)}"
            )

    elif use_images:
        try:
            image_paths = []
            for i, url in enumerate(image_urls):
                img_path = os.path.join(IMAGE_DIR, f"{job_id}_img{i}.jpg")
                download_file(url, img_path)
                image_paths.append(img_path)

            bg_video_path = os.path.join(IMAGE_DIR, f"{job_id}_bg.mp4")
            build_background_from_images(image_paths, bg_video_path, final_duration, job_id)

            overlay_filter = "eq=contrast=1.04:saturation=1.10"
            video_filter = compose_video_filter(overlay_filter)
            render_mode = f"static_image_clean_{len(image_paths)}"
            media_count = len(image_paths)

            ffmpeg_cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", bg_video_path,
                "-i", final_audio_path,
                "-vf", video_filter,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-t", f"{final_duration:.2f}",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "28",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "44100",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                video_path
            ]

        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"build_background_from_images fallo: {str(e)}"
            )

    else:
        video_filter = compose_video_filter()

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-f", "lavfi",
            "-i", f"color=c=black:s=720x1280:r=24:d={final_duration}",
            "-i", final_audio_path,
            "-vf", video_filter,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", f"{final_duration:.2f}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            video_path
        ]

    validate_video_filter_for_ffmpeg(video_filter)

    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Error renderizando video",
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "render_mode": render_mode,
                "video_filter_excerpt": video_filter[:2500],
            }
        )

    if not os.path.exists(video_path):
        raise HTTPException(
            status_code=500,
            detail={
                "message": "El video no se generó",
                "render_mode": render_mode,
            }
        )

    base_url = os.environ.get(
        "BASE_URL",
        "https://ffmpeg-render-api-productionpod.up.railway.app"
    )

    return {
        "ok": True,
        "video_url": f"/video/{job_id}.mp4",
        "video_url_full": f"{base_url}/video/{job_id}.mp4",
        "voice_duration": voice_duration,
        "audio_duration": final_audio_duration,
        "final_duration": final_duration,
        "end_tail_duration": END_TAIL_DURATION,
        "scene_duration_mode": "pod_single_avatar_video_no_tail",
        "max_clone_pad_per_scene": MAX_CLONE_PAD_PER_SCENE,
        "cta_start_time": None,
        "subtitles_mode_received": data.subtitles_mode,
        "render_mode": render_mode,
        "cues_count": len(cues),
        "speed_factor": speed_factor,
        "music_used": os.path.exists(MUSIC_FILE),
        "media_count": media_count,
        "referencia_biblica_used": bool(data.referencia_biblica and data.referencia_biblica.strip()),
        "hook_received": bool(data.hook and data.hook.strip()),
        "hook_visual_text_received": bool(data.hook_visual_text and data.hook_visual_text.strip()),
        "call_to_action_received": bool(data.call_to_action and data.call_to_action.strip()),
        "effective_call_to_action": effective_call_to_action,
        "cta_rendered": False,
        "truth_punch_text": resolved_truth_punch,
        "truth_punch_text_received": bool(data.truth_punch_text and data.truth_punch_text.strip()),
        "truth_punch_duration": TRUTH_PUNCH_DURATION,
        "truth_punch_start_time": truth_punch_window[0] if truth_punch_window else None,
        "truth_punch_end_time": truth_punch_window[1] if truth_punch_window else None,
        "hook_card_mode": "disabled_no_cards",
        "truth_punch_mode": "disabled_for_pod",
        "cta_card_mode": "disabled_no_cards",
        "cta_detection_mode": "disabled_no_visual_cta",
        "hook_word_1_start": HOOK_WORD_1_START,
        "hook_word_2_start": HOOK_WORD_2_START,
        "hook_word_3_start": HOOK_WORD_3_START,
        "voice_starts_at": "0.00s",
        "sfx_enabled": False,
        "music_required": False,
        "cta_card_required": False,
        "render_style": "pod_tiktok_avatar_captioned",
    }
