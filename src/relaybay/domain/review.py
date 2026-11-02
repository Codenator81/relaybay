"""Review: the acceptance checklist of a task and its screenshots, plus the file safety rules
(types by magic bytes, allowed folders, limits) that every stored file passes."""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from relaybay.domain.errors import ValidationError

STEP_STATUSES = ("open", "accepted", "rejected")
STEP_TITLE_MAX = 200
STEP_DETAILS_MAX = 10_000
STEPS_MAX = 50
CAPTION_MAX = 2_000
ORIGINAL_NAME_MAX = 200
DEFAULT_FILE_MAX_MB = 10
DEFAULT_TASK_MAX_MB = 200
MB = 1024 * 1024

# Only raster images. SVG is refused on purpose: it is a document that can carry script.
IMAGE_TYPES = (("image/png", ".png"), ("image/jpeg", ".jpg"), ("image/webp", ".webp"))
IMAGE_TYPES += (("image/gif", ".gif"),)
EXTENSIONS = dict(IMAGE_TYPES)


@dataclass(slots=True)
class Attachment:
    id: int
    task_id: int
    step_id: int | None
    kind: str
    stored_name: str
    original_name: str | None
    mime: str
    size: int
    sha256: str
    caption_md: str | None
    created_by: str
    created_at: str
    task_key: str = ""
    project_slug: str = ""
    step_position: int | None = None  # the number of its step in the checklist


@dataclass(slots=True)
class ReviewStep:
    id: int
    task_id: int
    position: int
    title: str
    details_md: str | None
    status: str
    decided_at: str | None
    decided_by: str | None
    created_by: str
    created_at: str
    updated_at: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(slots=True)
class Review:
    """A task's checklist with its screenshots; `loose` are the ones of no step."""

    steps: list[ReviewStep]
    loose: list[Attachment]

    @property
    def accepted(self) -> int:
        return sum(1 for s in self.steps if s.status == "accepted")

    @property
    def rejected(self) -> int:
        return sum(1 for s in self.steps if s.status == "rejected")

    def images(self) -> list[Attachment]:
        """Every screenshot in viewer order: step by step, then the loose ones."""
        return [a for s in self.steps for a in s.attachments] + self.loose


def sniff_image(head: bytes) -> str | None:
    """The image type by its magic bytes, never by a file name or a claimed type."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def require_image(data: bytes) -> str:
    mime = sniff_image(data[:16])
    if mime is None:
        raise ValidationError(
            "Not a PNG, JPEG, WebP or GIF image (checked by its content, not its name); SVG"
            " and other files are refused."
        )
    return mime


def clean_step_title(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValidationError("A review step needs a title.")
    if len(text) > STEP_TITLE_MAX:
        raise ValidationError(f"A step title is longer than {STEP_TITLE_MAX} characters.")
    return text


def clean_markdown(value: object, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string.")
    text = value.replace("\r\n", "\n").strip()
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters.")
    return text or None


def clean_original_name(value: object) -> str | None:
    if value is None:
        return None
    # Only the last component, for display: never used to build a path.
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:ORIGINAL_NAME_MAX] or None


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _network_or_device(text: str) -> bool:
    """A path that starts with two slashes or backslashes: a UNC share, a `?` or `.` device
    path, a named pipe. Reading one can reach another machine or block, so it is refused before
    the disk is touched."""
    return text.replace("/", "\\").startswith("\\\\")


def _fold(text: str) -> str:
    return text.replace("/", "\\").rstrip("\\").casefold()


def _under_configured(text: str, roots: list[Path]) -> bool:
    """`text` starts with one of `roots` exactly as configured (compared as text, nothing is
    resolved): the owner named that share on purpose."""
    folded = _fold(text)
    for root in roots:
        prefix = _fold(str(root))
        if prefix and (folded == prefix or folded.startswith(prefix + "\\")):
            return True
    return False


def resolve_source(
    raw: str, roots: list[Path], refused: list[Path], private: Path | None = None
) -> tuple[Path, os.stat_result]:
    """The real path of a file an agent asked to attach and its stat: absolute, symlinks and
    `..` resolved before the check, inside one of `roots`, outside every `refused` folder and
    not directly in `private` (the data folder: database, token), a regular file (not a
    device, pipe or folder). Network shares and device paths are refused by their prefix unless
    one of `roots` is that share. Errors say why without echoing other paths."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("path must not be empty.")
    if _network_or_device(text) and not _under_configured(text, roots):
        raise ValidationError(
            f"{text!r} is a network share or a device path; relaybay reads only local files"
            " unless that share is listed in `attachment_roots`."
        )
    given = Path(text).expanduser()
    if not given.is_absolute():
        raise ValidationError(f"path must be absolute, got {text!r}.")
    try:
        real = given.resolve(strict=True)
        info = real.stat()
    except FileNotFoundError:
        raise ValidationError(f"File {text!r} not found.") from None
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop
        raise ValidationError(f"Cannot read {text!r}: {exc}.") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"{text!r} is not a regular file.")
    resolved_roots = []
    for root in roots:
        try:
            resolved_roots.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not any(_inside(real, root) for root in resolved_roots):
        raise ValidationError(
            f"{text!r} is outside the folders relaybay may read (the project's repository, the"
            " temp folder and `attachment_roots`); copy the file there or send content_base64."
        )
    for folder in refused:
        try:
            if _inside(real, folder.resolve()):
                raise ValidationError(f"{text!r} is inside relaybay's data folder.")
        except (OSError, RuntimeError):
            continue
    if private is not None and real.parent == private.resolve():
        raise ValidationError(f"{text!r} is inside relaybay's data folder.")
    return real, info


def read_source(path: Path, checked: os.stat_result, limit: int) -> bytes:
    """Read a resolved file, at most `limit` bytes. The opened file must be the one
    `resolve_source` checked (`checked` is its stat): one swapped in between is refused."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValidationError(f"Cannot open the file: {exc.strerror or exc}.") from None
    with os.fdopen(fd, "rb") as fh:
        opened = os.fstat(fh.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(checked, opened):
            raise ValidationError("The file changed while it was being read; try again.")
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ValidationError(f"The file is larger than {limit // MB} MB.")
    if not data:
        raise ValidationError("The file is empty.")
    return data
