import hashlib
import json
import os
import secrets
import shutil
import struct
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

MAGIC = b"LKBX"
VERSION = 1
EXTENSION = ".lockbox"
CHUNK = 1 << 20
TAG = 16
HEADER = struct.Struct(">4sBBIIB7s16s")
USES_KEYFILE = 1
IS_FOLDER = 2
MIN_PASSWORD = 8
DEFAULT_MEMORY = 131072
DEFAULT_ITERATIONS = 3
LANES = 4
MAX_MEMORY = 1 << 20
MAX_ITERATIONS = 20
MAX_LANES = 16
DATA = 0
LAST = 1
META = 2
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % n for n in range(1, 10)} | {"LPT%d" % n for n in range(1, 10)}
PASSWORD_CHARS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!#$%&*+-=?@"


class WrongPassword(Exception):
    pass


class Damaged(Exception):
    pass


class NotLockbox(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Result:
    source: Path
    action: str
    output: Path = None
    error: str = ""
    size: int = 0


def is_locked(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == MAGIC
    except OSError:
        return False


def strength(password):
    score = 0
    if len(password) >= 8:
        score += 1
    if len(password) >= 12:
        score += 1
    if len(password) >= 16:
        score += 1
    groups = [
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ]
    if sum(groups) >= 3:
        score += 1
    return min(score, 4)


def generate_password(length=20):
    return "".join(secrets.choice(PASSWORD_CHARS) for _ in range(length))


def hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                return digest.digest()
            digest.update(block)


def make_key(password, keyfile, salt, memory, iterations, lanes):
    material = unicodedata.normalize("NFKC", password).encode("utf-8")
    if keyfile:
        material += b"\x00" + hash_file(keyfile)
    kdf = Argon2id(salt=salt, length=32, iterations=iterations, lanes=lanes, memory_cost=memory)
    return kdf.derive(material)


def make_nonce(prefix, counter, kind):
    return prefix + struct.pack(">IB", counter, kind)


def unique_path(path):
    path = Path(path)
    if not path.exists():
        return path
    number = 1
    while True:
        candidate = path.with_name("%s (%d)%s" % (path.stem, number, path.suffix))
        if not candidate.exists():
            return candidate
        number += 1


def safe_name(name):
    name = Path(str(name).replace("\\", "/")).name
    name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
    if not name or name.split(".")[0].upper() in RESERVED:
        return "unlocked"
    return name


def wipe(path):
    path = Path(path)
    size = path.stat().st_size
    with open(path, "r+b") as handle:
        left = size
        while left > 0:
            piece = min(left, 1 << 20)
            handle.write(os.urandom(piece))
            left -= piece
        handle.flush()
        os.fsync(handle.fileno())
    path.unlink()


def remove_original(path, shred):
    path = Path(path)
    if path.is_dir():
        if shred:
            for child in path.rglob("*"):
                if child.is_file():
                    wipe(child)
        shutil.rmtree(path)
    elif shred:
        wipe(path)
    else:
        path.unlink()


def seal(src, dst, password, keyfile, name, is_folder, memory, iterations, progress, cancel):
    salt = secrets.token_bytes(16)
    prefix = secrets.token_bytes(7)
    flags = (USES_KEYFILE if keyfile else 0) | (IS_FOLDER if is_folder else 0)
    header = HEADER.pack(MAGIC, VERSION, flags, memory, iterations, LANES, prefix, salt)
    box = AESGCM(make_key(password, keyfile, salt, memory, iterations, LANES))
    total = os.path.getsize(src)
    meta = json.dumps({"name": name, "size": total}).encode()
    with open(src, "rb") as source, open(dst, "wb") as out:
        out.write(header)
        sealed = box.encrypt(make_nonce(prefix, 0, META), meta, header)
        out.write(struct.pack(">H", len(sealed)) + sealed)
        counter = 0
        done = 0
        block = source.read(CHUNK)
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            ahead = source.read(CHUNK)
            last = not ahead
            out.write(box.encrypt(make_nonce(prefix, counter, LAST if last else DATA), block, header))
            done += len(block)
            if progress:
                progress(done, total)
            if last:
                break
            block = ahead
            counter += 1


def read_header(handle):
    raw = handle.read(HEADER.size)
    if len(raw) < HEADER.size:
        raise NotLockbox("This is not a Lockbox file")
    magic, version, flags, memory, iterations, lanes, prefix, salt = HEADER.unpack(raw)
    if magic != MAGIC:
        raise NotLockbox("This is not a Lockbox file")
    if version != VERSION:
        raise NotLockbox("This file was made by a newer version of Lockbox")
    if not (8 * max(lanes, 1) <= memory <= MAX_MEMORY and 1 <= iterations <= MAX_ITERATIONS and 1 <= lanes <= MAX_LANES):
        raise Damaged("The file header is damaged")
    return raw, flags, memory, iterations, lanes, prefix, salt


def unseal(src, password, keyfile, sink, progress, cancel):
    size = os.path.getsize(src)
    with open(src, "rb") as handle:
        header, flags, memory, iterations, lanes, prefix, salt = read_header(handle)
        if flags & USES_KEYFILE and not keyfile:
            raise WrongPassword("This file also needs its key file")
        box = AESGCM(make_key(password, keyfile, salt, memory, iterations, lanes))
        raw_length = handle.read(2)
        if len(raw_length) < 2:
            raise Damaged("The file is cut short")
        sealed = handle.read(struct.unpack(">H", raw_length)[0])
        try:
            meta = json.loads(box.decrypt(make_nonce(prefix, 0, META), sealed, header))
        except (InvalidTag, ValueError):
            raise WrongPassword("Wrong password" + (" or key file" if flags & USES_KEYFILE else ""))
        total = int(meta.get("size", 0))
        counter = 0
        done = 0
        step = CHUNK + TAG
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            last = size - handle.tell() <= step
            block = handle.read(step)
            try:
                plain = box.decrypt(make_nonce(prefix, counter, LAST if last else DATA), block, header)
            except InvalidTag:
                raise Damaged("The file is damaged, cut short or has been changed")
            if sink:
                sink(plain)
            done += len(plain)
            if progress:
                progress(done, total)
            if last:
                break
            counter += 1
    return meta, flags


def zip_folder(folder, dst, cancel):
    folder = Path(folder)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if path.is_symlink():
                continue
            archive.write(path, path.relative_to(folder).as_posix() + ("/" if path.is_dir() else ""))


def unzip_folder(archive_path, dest):
    dest = Path(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (dest / member.filename).resolve()
            if root != target and root not in target.parents:
                raise Damaged("The archive tries to write outside its folder")
        archive.extractall(dest)


def encrypt_path(src, password, keyfile=None, output_dir=None, hide_name=False, delete_original=False,
                 shred=False, memory=DEFAULT_MEMORY, iterations=DEFAULT_ITERATIONS, progress=None, cancel=None):
    src = Path(src)
    result = Result(source=src, action="lock")
    temp = None
    staged = None
    try:
        if len(password) < MIN_PASSWORD:
            raise ValueError("The password needs at least %d characters" % MIN_PASSWORD)
        if keyfile and not Path(keyfile).is_file():
            raise ValueError("The key file could not be found")
        if not src.exists():
            raise ValueError("The file could not be found")
        if src.is_file() and is_locked(src):
            raise ValueError("This file is already locked")
        folder = Path(output_dir) if output_dir else src.parent
        folder.mkdir(parents=True, exist_ok=True)
        name = secrets.token_hex(8) if hide_name else src.name
        target = unique_path(folder / (name + EXTENSION))
        handle, temp = tempfile.mkstemp(dir=folder, prefix=".lockbox-", suffix=".tmp")
        os.close(handle)
        source = src
        if src.is_dir():
            handle, staged = tempfile.mkstemp(dir=folder, prefix=".lockbox-", suffix=".zip")
            os.close(handle)
            zip_folder(src, staged, cancel)
            source = Path(staged)
        seal(source, temp, password, keyfile, src.name, src.is_dir(), memory, iterations, progress, cancel)
        os.replace(temp, target)
        temp = None
        if delete_original:
            unseal(target, password, keyfile, None, None, cancel)
            remove_original(src, shred)
        result.output = target
        result.size = target.stat().st_size
    except WrongPassword as problem:
        result.error = str(problem)
    except Cancelled:
        result.error = "Cancelled"
    except (ValueError, Damaged, NotLockbox, OSError) as problem:
        result.error = str(problem)
    finally:
        for leftover in (temp, staged):
            if leftover and os.path.exists(leftover):
                os.remove(leftover)
    return result


def decrypt_path(src, password, keyfile=None, output_dir=None, delete_original=False, shred=False,
                 progress=None, cancel=None):
    src = Path(src)
    result = Result(source=src, action="unlock")
    temp = None
    try:
        if not src.is_file():
            raise ValueError("The file could not be found")
        if keyfile and not Path(keyfile).is_file():
            raise ValueError("The key file could not be found")
        folder = Path(output_dir) if output_dir else src.parent
        folder.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=folder, prefix=".lockbox-", suffix=".tmp")
        with os.fdopen(handle, "wb") as out:
            meta, flags = unseal(src, password, keyfile, out.write, progress, cancel)
        name = safe_name(meta.get("name", ""))
        target = unique_path(folder / name)
        if flags & IS_FOLDER:
            unzip_folder(temp, target)
            os.remove(temp)
        else:
            os.replace(temp, target)
        temp = None
        if delete_original:
            remove_original(src, shred)
        result.output = target
        result.size = int(meta.get("size", 0))
    except WrongPassword as problem:
        result.error = str(problem)
    except Cancelled:
        result.error = "Cancelled"
    except (ValueError, Damaged, NotLockbox, OSError, zipfile.BadZipFile) as problem:
        result.error = str(problem)
    finally:
        if temp and os.path.exists(temp):
            os.remove(temp)
    return result


def describe(path):
    with open(path, "rb") as handle:
        header, flags, memory, iterations, lanes, prefix, salt = read_header(handle)
    return {
        "version": VERSION,
        "memory_mib": memory // 1024,
        "iterations": iterations,
        "lanes": lanes,
        "key_file": bool(flags & USES_KEYFILE),
        "folder": bool(flags & IS_FOLDER),
        "bytes": os.path.getsize(path),
    }


def human_size(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%d B" % value if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024

_credit = "Built by Unkn0wngb"
