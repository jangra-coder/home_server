#!/usr/bin/env python3
"""
server_brain.py -- voice-facing download status probe.

Executed by a Node-RED `exec` node, which is in turn triggered by an Alexa
Routine. Reads the live progress of the background Telegram downloader bot and
prints a single TTS-ready sentence on stdout for the Echo to speak.

Design constraints (each one is a scar from a real failure -- see
docs/01-telegram-alexa-voice.md):

  1. Runs inside the slim Node-RED container, where `pip --user` installs land
     in a site directory that is NOT on the default sys.path. Hence the user
     site bootstrap below, which must precede the telethon import.
  2. The downloader daemon holds an exclusive SQLite write lock on its
     .session file. We never open the original -- we copy it (plus its WAL
     sidecars) to a scratch path first.
  3. Telethon is imported lazily, inside the fetch path, so this module stays
     importable for unit tests on machines without telethon.
  4. It always prints something speakable and exits 0 by default. A non-zero
     exit makes the Node-RED exec node take its error branch, which leaves the
     Echo silent -- the single worst outcome for a voice command.

Usage:
    python3 server_brain.py                 # English sentence on stdout
    python3 server_brain.py --lang hinglish
    python3 server_brain.py --json          # structured output for debugging
"""

# --------------------------------------------------------------------------
# USER SITE BOOTSTRAP -- must stay above any third-party import.
# `pip install --user` inside the Node-RED image writes to
# /usr/src/node-red/.local/lib/pythonX.Y/site-packages, which is not on
# sys.path for a bare `python3 foo.py` invocation. Without this, telethon
# installs "successfully" and then fails with ModuleNotFoundError.
# --------------------------------------------------------------------------
import site
import sys


def _ensure_user_site_packages() -> None:
    """Put the per-user site-packages dir on sys.path if it is missing."""
    try:
        user_site = site.getusersitepackages()
    except Exception:  # pragma: no cover - only fires on exotic interpreters
        return
    # getusersitepackages() returns a str on CPython but a list on some
    # distro-patched builds; normalise both shapes.
    candidates = [user_site] if isinstance(user_site, str) else list(user_site or [])
    for path in candidates:
        if path and path not in sys.path:
            sys.path.append(path)


_ensure_user_site_packages()

# --------------------------------------------------------------------------
# Standard library imports only from here down. Telethon is imported lazily.
# --------------------------------------------------------------------------
import argparse  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from dataclasses import asdict, dataclass, field  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

__all__ = [
    "Config",
    "DownloadStatus",
    "parse_progress_message",
    "parse_size",
    "parse_eta",
    "speakable_name",
    "render_speech",
    "pick_best",
]


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Runtime configuration, entirely environment driven.

    Defaults match the deployed layout: the host directory
    /home/dell_jan/telegram-downloader is bind-mounted into the Node-RED
    container at /mnt/telegram_bot (read-only).
    """

    bot_dir: str = "/mnt/telegram_bot"
    session_name: str = "downloader"
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    # Chat the downloader posts its progress messages into. Accepts a numeric
    # id, an @username, or "me" for Saved Messages.
    status_chat: str = "me"
    message_scan_limit: int = 25
    # A progress message older than this is treated as stale rather than live.
    stale_after_seconds: int = 120
    # Where the bot writes incremental .part files, as seen from this process.
    downloads_dir: str = "/mnt/telegram_bot/downloads"
    # Seconds to watch a .part file grow when falling back to disk probing.
    part_sample_seconds: float = 1.5
    telegram_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Config":
        env = os.environ if env is None else env

        def _int(key: str, default: Optional[int]) -> Optional[int]:
            raw = env.get(key)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def _float(key: str, default: float) -> float:
            raw = env.get(key)
            if raw is None or raw.strip() == "":
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        bot_dir = env.get("TG_BOT_DIR", cls.bot_dir)
        return cls(
            bot_dir=bot_dir,
            session_name=env.get("TG_SESSION_NAME", cls.session_name),
            api_id=_int("TG_API_ID", None),
            api_hash=env.get("TG_API_HASH") or None,
            status_chat=env.get("TG_STATUS_CHAT", cls.status_chat),
            message_scan_limit=_int("TG_SCAN_LIMIT", cls.message_scan_limit) or cls.message_scan_limit,
            stale_after_seconds=_int("TG_STALE_AFTER", cls.stale_after_seconds) or cls.stale_after_seconds,
            downloads_dir=env.get("TG_DOWNLOADS_DIR", os.path.join(bot_dir, "downloads")),
            part_sample_seconds=_float("TG_PART_SAMPLE_SECONDS", cls.part_sample_seconds),
            telegram_timeout_seconds=_float("TG_TIMEOUT", cls.telegram_timeout_seconds),
        )

    @property
    def session_path(self) -> str:
        name = self.session_name
        if not name.endswith(".session"):
            name += ".session"
        return os.path.join(self.bot_dir, name)


# --------------------------------------------------------------------------
# Status model
# --------------------------------------------------------------------------


@dataclass
class DownloadStatus:
    """One download's state, normalised across every source we can read."""

    state: str = "idle"  # idle | active | stalled | complete | error
    filename: Optional[str] = None
    percent: Optional[float] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    speed_bps: Optional[float] = None
    eta_seconds: Optional[int] = None
    # Seconds since the underlying evidence (message edit / file mtime) changed.
    age_seconds: Optional[int] = None
    queued: int = 0
    source: str = "none"  # telegram | disk | none
    detail: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

# Telegram downloader bots overwhelmingly format sizes with a 1024-based
# `humanbytes()` helper even when they print "MB", so both the decimal and the
# IEC spellings map to powers of 1024 here. The difference is ~7% at the GB
# mark, which is inaudible in a spoken sentence.
_UNIT_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
}

_SIZE_RE = r"(\d+(?:[.,]\d+)?)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)"

# "1.2 GB of 2.8 GB", "1.2GB / 2.8GB", "1.2 GiB out of 2.8 GiB"
_PROGRESS_PAIR_RE = re.compile(
    _SIZE_RE + r"\s*(?:of|/|out of)\s*" + _SIZE_RE,
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_SPEED_RE = re.compile(_SIZE_RE + r"\s*(?:/|per\s+)s(?:ec(?:ond)?)?\b", re.IGNORECASE)
_ETA_CLOCK_RE = re.compile(
    r"\bETA\b\D{0,4}(?:(\d+):)?(\d{1,2}):(\d{2})\b", re.IGNORECASE
)
_ETA_WORDS_RE = re.compile(
    r"\bETA\b\D{0,4}((?:\d+\s*[hms]\s*)+)", re.IGNORECASE
)
_ETA_UNIT_RE = re.compile(r"(\d+)\s*([hms])", re.IGNORECASE)

_FILENAME_LABEL_RE = re.compile(
    r"^\s*(?:\W*\s*)?(?:file\s*name|filename|file|name|downloading|download)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)
_MEDIA_EXT_RE = re.compile(
    r"\b([\w\-.\[\]()'! ]{2,150}?\.(?:mkv|mp4|avi|mov|m4v|ts|webm|flv|wmv|mpg|mpeg|"
    r"zip|rar|7z|iso|pdf|epub|mp3|flac|m4a))\b",
    re.IGNORECASE,
)

_COMPLETE_RE = re.compile(
    r"\b(download\s*(?:ed|complete[d]?|finished)|completed|finished|"
    r"successfully\s+downloaded|upload\s*complete[d]?|done)\b",
    re.IGNORECASE,
)
_ACTIVITY_RE = re.compile(
    r"\b(download(?:ing)?|fetch(?:ing)?|progress|receiv(?:ing|ed)|"
    r"upload(?:ing)?|remaining|eta|speed)\b",
    re.IGNORECASE,
)
_QUEUE_RE = re.compile(
    r"\b(?:queue[d]?|pending|waiting)\b\D{0,12}(\d+)|(\d+)\s+(?:file[s]?\s+)?"
    r"(?:in\s+)?queue\b",
    re.IGNORECASE,
)


def parse_size(value: str, unit: str) -> Optional[int]:
    """Convert a ('1.2', 'GB') pair into a byte count."""
    try:
        number = float(value.replace(",", "."))
    except (TypeError, ValueError):
        return None
    multiplier = _UNIT_MULTIPLIERS.get((unit or "").strip().lower())
    if multiplier is None:
        return None
    return int(number * multiplier)


def parse_eta(text: str) -> Optional[int]:
    """Pull an ETA out of a progress line, in seconds.

    Understands `ETA 00:04:53`, `ETA: 4m 53s` and `ETA - 90s`.
    """
    if not text:
        return None
    clock = _ETA_CLOCK_RE.search(text)
    if clock:
        hours = int(clock.group(1) or 0)
        minutes = int(clock.group(2))
        seconds = int(clock.group(3))
        return hours * 3600 + minutes * 60 + seconds
    words = _ETA_WORDS_RE.search(text)
    if words:
        total = 0
        found = False
        for amount, unit in _ETA_UNIT_RE.findall(words.group(1)):
            found = True
            scale = {"h": 3600, "m": 60, "s": 1}[unit.lower()]
            total += int(amount) * scale
        if found:
            return total
    return None


def _extract_filename(text: str) -> Optional[str]:
    """Best-effort filename extraction from a progress message."""
    for line in text.splitlines():
        labelled = _FILENAME_LABEL_RE.match(line)
        if labelled:
            candidate = labelled.group(1).strip().strip("`*_ ")
            # A label line that is really just a status word ("Downloading: 42%")
            # is not a filename.
            if candidate and not _PERCENT_RE.fullmatch(candidate.strip()):
                return candidate
    media = _MEDIA_EXT_RE.search(text)
    if media:
        return media.group(1).strip()
    return None


def parse_progress_message(text: str) -> Optional[DownloadStatus]:
    """Parse one Telegram message into a DownloadStatus.

    Returns None when the message carries no download signal at all, so the
    caller can skip chatter (help text, "/start", error replies, ...).
    """
    if not text or not text.strip():
        return None

    percent_match = _PERCENT_RE.search(text)
    pair_match = _PROGRESS_PAIR_RE.search(text)
    speed_match = _SPEED_RE.search(text)
    eta = parse_eta(text)
    looks_like_activity = bool(_ACTIVITY_RE.search(text))

    has_signal = bool(percent_match or pair_match or speed_match or eta is not None)
    if not has_signal:
        # No numbers at all -- only accept an explicit completion notice.
        if looks_like_activity and _COMPLETE_RE.search(text):
            return DownloadStatus(
                state="complete",
                filename=_extract_filename(text),
                percent=100.0,
                source="telegram",
            )
        return None

    if not looks_like_activity:
        # Numbers without any download vocabulary -- probably unrelated chatter.
        return None

    status = DownloadStatus(state="active", source="telegram")
    status.filename = _extract_filename(text)

    if percent_match:
        try:
            status.percent = float(percent_match.group(1).replace(",", "."))
        except ValueError:
            status.percent = None

    if pair_match:
        status.downloaded_bytes = parse_size(pair_match.group(1), pair_match.group(2))
        status.total_bytes = parse_size(pair_match.group(3), pair_match.group(4))
        if status.percent is None and status.total_bytes:
            status.percent = round(
                100.0 * (status.downloaded_bytes or 0) / status.total_bytes, 1
            )

    if speed_match:
        speed = parse_size(speed_match.group(1), speed_match.group(2))
        status.speed_bps = float(speed) if speed is not None else None

    status.eta_seconds = eta

    # Derive an ETA when the bot did not print one but gave us the arithmetic.
    if (
        status.eta_seconds is None
        and status.speed_bps
        and status.total_bytes
        and status.downloaded_bytes is not None
    ):
        remaining = status.total_bytes - status.downloaded_bytes
        if remaining > 0 and status.speed_bps > 0:
            status.eta_seconds = int(remaining / status.speed_bps)

    queue_match = _QUEUE_RE.search(text)
    if queue_match:
        raw = queue_match.group(1) or queue_match.group(2)
        try:
            status.queued = int(raw)
        except (TypeError, ValueError):
            status.queued = 0

    if _COMPLETE_RE.search(text) and (status.percent is None or status.percent >= 99.5):
        status.state = "complete"
        status.percent = 100.0
    elif status.percent is not None and status.percent >= 100.0:
        status.state = "complete"

    return status


def pick_best(candidates: List[DownloadStatus]) -> Optional[DownloadStatus]:
    """Choose the status to speak when several messages parsed successfully.

    An in-flight download always outranks a finished one, and among equals the
    freshest evidence wins.
    """
    if not candidates:
        return None

    def rank(status: DownloadStatus) -> tuple:
        state_rank = {"active": 3, "stalled": 2, "complete": 1}.get(status.state, 0)
        freshness = -(status.age_seconds if status.age_seconds is not None else 10**9)
        return (state_rank, freshness)

    return max(candidates, key=rank)


# --------------------------------------------------------------------------
# Session handling -- copy around the daemon's exclusive SQLite lock
# --------------------------------------------------------------------------


def copy_session(session_path: str, scratch_dir: Optional[str] = None) -> str:
    """Copy a Telethon .session (and its WAL sidecars) to a scratch path.

    The downloader daemon keeps an exclusive write lock on the live session
    file; opening it directly raises `sqlite3.OperationalError: database is
    locked` and, worse, risks corrupting the daemon's auth state. Copying is
    the only safe read path.

    Returns the path to the copy. Raises FileNotFoundError if the session is
    missing.
    """
    if not os.path.isfile(session_path):
        raise FileNotFoundError(f"Telegram session not found: {session_path}")

    scratch_dir = scratch_dir or tempfile.mkdtemp(prefix="server_brain_")
    os.makedirs(scratch_dir, exist_ok=True)
    target = os.path.join(scratch_dir, os.path.basename(session_path))
    shutil.copyfile(session_path, target)

    # SQLite in WAL mode keeps committed pages in the -wal file; copying the
    # main database alone can yield a torn or stale read.
    for suffix in ("-wal", "-shm"):
        sidecar = session_path + suffix
        if os.path.isfile(sidecar):
            shutil.copyfile(sidecar, target + suffix)

    return target


# --------------------------------------------------------------------------
# Source 1: Telegram
# --------------------------------------------------------------------------


def fetch_from_telegram(cfg: Config) -> DownloadStatus:
    """Read recent messages from the status chat and parse the newest progress.

    Telethon is imported here, not at module scope, so the pure-parsing helpers
    above stay importable (and testable) without it installed.
    """
    scratch_dir = tempfile.mkdtemp(prefix="server_brain_")
    try:
        try:
            session_copy = copy_session(cfg.session_path, scratch_dir)
        except FileNotFoundError as exc:
            return DownloadStatus(state="error", detail=str(exc), source="telegram")

        try:
            from telethon.sync import TelegramClient  # noqa: WPS433 (lazy on purpose)
        except ImportError as exc:
            return DownloadStatus(
                state="error",
                detail=f"telethon is not importable: {exc}",
                source="telegram",
            )

        if not cfg.api_id or not cfg.api_hash:
            return DownloadStatus(
                state="error",
                detail="TG_API_ID / TG_API_HASH are not set",
                source="telegram",
            )

        # Telethon appends ".session" itself, so hand it the stem.
        session_stem = session_copy[: -len(".session")] if session_copy.endswith(".session") else session_copy

        statuses: List[DownloadStatus] = []
        now = time.time()
        try:
            with TelegramClient(
                session_stem,
                cfg.api_id,
                cfg.api_hash,
                timeout=cfg.telegram_timeout_seconds,
            ) as client:
                chat = _resolve_chat(cfg.status_chat)
                for message in client.iter_messages(chat, limit=cfg.message_scan_limit):
                    text = getattr(message, "message", None) or getattr(message, "text", None)
                    parsed = parse_progress_message(text or "")
                    if parsed is None:
                        continue
                    # Prefer edit time: progress messages are edited in place.
                    stamp = getattr(message, "edit_date", None) or getattr(message, "date", None)
                    if stamp is not None:
                        try:
                            parsed.age_seconds = max(0, int(now - stamp.timestamp()))
                        except (AttributeError, OverflowError, OSError):
                            parsed.age_seconds = None
                    statuses.append(parsed)
        except Exception as exc:  # noqa: BLE001 - never let a voice command crash
            return DownloadStatus(
                state="error",
                detail=f"{type(exc).__name__}: {exc}",
                source="telegram",
            )

        best = pick_best(statuses)
        if best is None:
            return DownloadStatus(state="idle", source="telegram")

        # A progress message that stopped being edited is not a live download.
        if (
            best.state == "active"
            and best.age_seconds is not None
            and best.age_seconds > cfg.stale_after_seconds
        ):
            best.state = "stalled"

        return best
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _resolve_chat(raw: str):
    """Interpret TG_STATUS_CHAT as an id, a @username, or 'me'."""
    value = (raw or "me").strip()
    if value.lower() == "me":
        return "me"
    try:
        return int(value)
    except ValueError:
        return value


# --------------------------------------------------------------------------
# Source 2: the .part files on disk (fallback / corroboration)
# --------------------------------------------------------------------------


def scan_part_files(downloads_dir: str, sample_seconds: float = 1.5) -> DownloadStatus:
    """Detect live downloads by watching the bot's incremental .part files grow.

    The downloader writes into `<name>.part` and renames on completion --
    precisely so Jellyfin's scanner ignores half-written media. That makes the
    .part set a reliable disk-side answer to "is anything downloading right
    now?" even when Telegram is unreachable.
    """
    if not os.path.isdir(downloads_dir):
        return DownloadStatus(
            state="error",
            detail=f"downloads dir not found: {downloads_dir}",
            source="disk",
        )

    pattern = os.path.join(glob.escape(downloads_dir), "**", "*.part")
    try:
        parts = [p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p)]
    except OSError as exc:
        return DownloadStatus(state="error", detail=str(exc), source="disk")

    if not parts:
        return DownloadStatus(state="idle", source="disk")

    def _size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    first = {p: _size(p) for p in parts}
    if sample_seconds > 0:
        time.sleep(sample_seconds)
    second = {p: _size(p) for p in parts}

    growing = {p: second[p] - first[p] for p in parts if second[p] > first[p]}
    target = max(growing, key=growing.get) if growing else max(parts, key=_size)

    now = time.time()
    try:
        age = max(0, int(now - os.path.getmtime(target)))
    except OSError:
        age = None

    status = DownloadStatus(
        state="active" if growing else "stalled",
        filename=os.path.basename(target),
        downloaded_bytes=second.get(target),
        age_seconds=age,
        queued=max(0, len(parts) - 1),
        source="disk",
    )
    if growing and sample_seconds > 0:
        status.speed_bps = growing[target] / sample_seconds
    status.extras["part_files"] = len(parts)
    return status


# --------------------------------------------------------------------------
# Rendering: bytes/seconds -> words an Echo can read aloud
# --------------------------------------------------------------------------

_RELEASE_JUNK = {
    "1080p", "720p", "480p", "2160p", "4k", "uhd", "x264", "x265", "h264", "h265",
    "hevc", "avc", "web", "webrip", "web-dl", "webdl", "bluray", "blu-ray", "brrip",
    "bdrip", "hdrip", "dvdrip", "hdtv", "aac", "ac3", "dts", "ddp", "dd5", "10bit",
    "8bit", "dual", "audio", "esub", "esubs", "msub", "msubs", "subs", "hindi",
    "english", "org", "remastered", "proper", "repack", "extended", "uncut", "hq",
    "hdr", "sdr", "amzn", "nf", "hmax", "ds4k",
}
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


def speakable_name(filename: Optional[str], max_words: int = 8) -> Optional[str]:
    """Turn `The.Movie.2024.1080p.x265-GRP.mkv.part` into `The Movie`.

    Release-scene filenames read horribly over TTS; strip the machinery and
    keep the part a human would actually say.
    """
    if not filename:
        return None
    name = os.path.basename(filename.strip())
    name = re.sub(r"\.part$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", name)  # media extension
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"[\[\](){}]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return None

    words: List[str] = []
    for word in name.split(" "):
        if _YEAR_RE.match(word):
            break  # everything past the year is release metadata
        if word.lower().strip("-") in _RELEASE_JUNK:
            break
        words.append(word)
        if len(words) >= max_words:
            break

    cleaned = " ".join(words).strip(" -")
    return cleaned or None


def human_bytes(num: Optional[float], lang: str = "en") -> Optional[str]:
    """Format a byte count for speech: 1288490188 -> '1.2 gigabytes'."""
    if num is None:
        return None
    units_en = [
        (1024**4, "terabyte", "terabytes"),
        (1024**3, "gigabyte", "gigabytes"),
        (1024**2, "megabyte", "megabytes"),
        (1024, "kilobyte", "kilobytes"),
    ]
    units_hi = [
        (1024**4, "टेराबाइट", "टेराबाइट"),
        (1024**3, "गीगाबाइट", "गीगाबाइट"),
        (1024**2, "मेगाबाइट", "मेगाबाइट"),
        (1024, "किलोबाइट", "किलोबाइट"),
    ]
    units = units_hi if lang == "hi" else units_en
    for scale, singular, plural in units:
        if num >= scale:
            value = num / scale
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text} {singular if text == '1' else plural}"
    unit = "बाइट" if lang == "hi" else "bytes"
    return f"{int(num)} {unit}"


def human_duration(seconds: Optional[int], lang: str = "en") -> Optional[str]:
    """Format a duration for speech, rounded to something worth saying."""
    if seconds is None or seconds < 0:
        return None
    words = {
        "en": {"h": ("hour", "hours"), "m": ("minute", "minutes"), "s": ("second", "seconds"), "lt": "less than a minute"},
        "hi": {"h": ("घंटा", "घंटे"), "m": ("मिनट", "मिनट"), "s": ("सेकंड", "सेकंड"), "lt": "एक मिनट से कम"},
        "hinglish": {"h": ("ghanta", "ghante"), "m": ("minute", "minute"), "s": ("second", "second"), "lt": "ek minute se kam"},
    }[lang if lang in ("en", "hi", "hinglish") else "en"]

    if seconds < 60:
        return words["lt"]
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        singular, plural = words["m"]
        return f"{minutes} {singular if minutes == 1 else plural}"
    hours = seconds // 3600
    rem_minutes = int(round((seconds % 3600) / 60.0))
    h_sing, h_plur = words["h"]
    head = f"{hours} {h_sing if hours == 1 else h_plur}"
    if rem_minutes:
        m_sing, m_plur = words["m"]
        head += f" {rem_minutes} {m_sing if rem_minutes == 1 else m_plur}"
    return head


def human_speed(speed_bps: Optional[float], lang: str = "en") -> Optional[str]:
    if not speed_bps or speed_bps <= 0:
        return None
    size = human_bytes(speed_bps, lang="hi" if lang == "hi" else "en")
    if size is None:
        return None
    suffix = {"en": "per second", "hi": "प्रति सेकंड", "hinglish": "per second"}.get(lang, "per second")
    return f"{size} {suffix}"


def render_speech(status: DownloadStatus, lang: str = "en") -> str:
    """Turn a DownloadStatus into one sentence an Echo can read aloud.

    Deliberately short: a voice answer that runs past ~12 seconds gets talked
    over. Percentage and ETA carry the information; sizes and speeds are only
    added when there is no ETA to give.
    """
    lang = lang if lang in ("en", "hi", "hinglish") else "en"
    name = speakable_name(status.filename)
    percent = int(round(status.percent)) if status.percent is not None else None
    eta = human_duration(status.eta_seconds, lang)
    speed = human_speed(status.speed_bps, lang)
    age = human_duration(status.age_seconds, lang)

    if status.state == "error":
        return {
            "en": "Sorry, I could not reach the downloader right now.",
            "hi": "माफ़ कीजिए, अभी डाउनलोडर से संपर्क नहीं हो पा रहा है।",
            "hinglish": "Sorry, abhi downloader se baat nahi ho pa rahi hai.",
        }[lang]

    if status.state == "idle":
        return {
            "en": "Nothing is downloading right now.",
            "hi": "अभी कोई डाउनलोड नहीं चल रहा है।",
            "hinglish": "Abhi koi download nahi chal raha hai.",
        }[lang]

    if status.state == "complete":
        if name:
            return {
                "en": f"{name} has finished downloading.",
                "hi": f"{name} का डाउनलोड पूरा हो चुका है।",
                "hinglish": f"{name} ka download complete ho chuka hai.",
            }[lang]
        return {
            "en": "The download has finished.",
            "hi": "डाउनलोड पूरा हो चुका है।",
            "hinglish": "Download complete ho chuka hai.",
        }[lang]

    if status.state == "stalled":
        pct_en = f" at {percent} percent" if percent is not None else ""
        pct_hi = f" {percent} प्रतिशत पर" if percent is not None else ""
        pct_hn = f" {percent} percent par" if percent is not None else ""
        stuck_en = f" It has not moved for {age}." if age else ""
        stuck_hi = f" पिछले {age} से कोई प्रगति नहीं हुई है।" if age else ""
        stuck_hn = f" Pichhle {age} se koi progress nahi hui hai." if age else ""
        subject_en = name or "The download"
        subject_hi = name or "डाउनलोड"
        subject_hn = name or "Download"
        return {
            "en": f"{subject_en} looks stuck{pct_en}.{stuck_en}".strip(),
            "hi": f"{subject_hi} अटका हुआ लग रहा है{pct_hi}।{stuck_hi}".strip(),
            "hinglish": f"{subject_hn} atka hua lag raha hai{pct_hn}.{stuck_hn}".strip(),
        }[lang]

    # state == "active"
    queued_en = f" {status.queued} more in the queue." if status.queued else ""
    queued_hi = f" कतार में {status.queued} और हैं।" if status.queued else ""
    queued_hn = f" Queue mein {status.queued} aur hain." if status.queued else ""

    if percent is not None:
        tail_en = f" About {eta} remaining." if eta else (f" Running at {speed}." if speed else "")
        tail_hi = f" लगभग {eta} बाकी है।" if eta else (f" गति {speed} है।" if speed else "")
        tail_hn = f" Lagbhag {eta} baaki hai." if eta else (f" Speed {speed} hai." if speed else "")
        subj_en = name or "A download"
        subj_hi = name or "एक डाउनलोड"
        subj_hn = name or "Ek download"
        return {
            "en": f"Yes, {subj_en} is at {percent} percent.{tail_en}{queued_en}",
            "hi": f"हाँ, {subj_hi} {percent} प्रतिशत तक पहुँच चुका है।{tail_hi}{queued_hi}",
            "hinglish": f"Haan, {subj_hn} {percent} percent tak pahunch chuka hai.{tail_hn}{queued_hn}",
        }[lang].strip()

    # Active but no percentage -- typically the disk fallback, which knows how
    # much has landed but not how big the file will be.
    done = human_bytes(status.downloaded_bytes, lang="hi" if lang == "hi" else "en")
    subj_en = name or "A download"
    subj_hi = name or "एक डाउनलोड"
    subj_hn = name or "Ek download"
    got_en = f" {done} so far." if done else ""
    got_hi = f" अब तक {done} आ चुका है।" if done else ""
    got_hn = f" Ab tak {done} aa chuka hai." if done else ""
    spd_en = f" Running at {speed}." if speed else ""
    spd_hi = f" गति {speed} है।" if speed else ""
    spd_hn = f" Speed {speed} hai." if speed else ""
    return {
        "en": f"Yes, {subj_en} is downloading.{got_en}{spd_en}{queued_en}",
        "hi": f"हाँ, {subj_hi} डाउनलोड हो रहा है।{got_hi}{spd_hi}{queued_hi}",
        "hinglish": f"Haan, {subj_hn} download ho raha hai.{got_hn}{spd_hn}{queued_hn}",
    }[lang].strip()


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def get_status(cfg: Config, prefer: str = "auto") -> DownloadStatus:
    """Resolve the download status from the best available source.

    'auto' asks Telegram first (it knows the total size, hence the ETA) and
    falls back to the .part files on disk when Telegram errors out or reports
    nothing -- the disk cannot lie about whether bytes are landing.
    """
    if prefer == "disk":
        return scan_part_files(cfg.downloads_dir, cfg.part_sample_seconds)

    telegram_status = fetch_from_telegram(cfg)
    if prefer == "telegram":
        return telegram_status

    if telegram_status.state in ("active", "complete"):
        return telegram_status

    disk_status = scan_part_files(cfg.downloads_dir, cfg.part_sample_seconds)
    if disk_status.state == "active":
        return disk_status
    if telegram_status.state == "error" and disk_status.state != "error":
        return disk_status
    if telegram_status.state == "stalled":
        return telegram_status
    return telegram_status if telegram_status.state != "error" else disk_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report Telegram downloader progress as a spoken sentence."
    )
    parser.add_argument(
        "--lang",
        default=os.environ.get("VOICE_LANG", "en"),
        choices=["en", "hi", "hinglish"],
        help="Language for the spoken reply (default: en).",
    )
    parser.add_argument(
        "--source",
        default="auto",
        choices=["auto", "telegram", "disk"],
        help="Where to read status from (default: auto).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the structured status as JSON instead of a sentence.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on error. Off by default so Node-RED always speaks.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_env()

    try:
        status = get_status(cfg, prefer=args.source)
    except Exception as exc:  # noqa: BLE001 - a crash here means Alexa says nothing
        status = DownloadStatus(state="error", detail=f"{type(exc).__name__}: {exc}")

    if args.json:
        payload = status.to_dict()
        payload["speech"] = render_speech(status, args.lang)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_speech(status, args.lang))

    if args.strict and status.state == "error":
        # The detail goes to stderr so it never reaches the Echo.
        print(f"error: {status.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
