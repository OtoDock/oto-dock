"""The Direct LLM file tools' operations, inside the session's sandbox.

This text never runs in the proxy. It is the program of
``files.PROGRAM``: the platform joins it behind the general loop
(``sandbox_program_runtime.py``) and the compute machine starts it in a
sandbox rendered from the session's role table and nothing else
(``sandbox_program.SandboxProgram``). So it imports nothing of the proxy,
uses the standard library only, runs on Python 3.10 and starts with no
``from __future__`` line (it is the second half of one text).

The kernel is the wall here: the sandbox holds the session's own folders,
read-only where its table says so, with empty folders over what is masked.
What is left for this text are the rules the kernel does not make:

1. A path is inside one of the role table's rows, also where it lands, and a
   link is followed only inside the row it starts in.
2. A refused name is refused, also where a path lands.
3. A path through a link is never opened: the answer names where it lands
   (``lands``), the platform's policy judges that place and asks again with
   ``strict``. Every open then walks from the root part by part with no link
   followed. A write never asks where a path lands: it is strict always.
4. Every open is made without blocking, and only a regular file is read or
   written.
5. A write is whole or not there: written beside the name and moved over
   it. The name is opened for writing first, so the kernel says whether this
   session may write that file, and the mounts say whether it may write
   there at all. The facts never do.

The rows, the refused names, the names a search skips, the caps of a read,
a write, a search and a skill, and the skills folder are the platform's
(the facts): this text holds none of them. The one number it holds is how
many skills a listing reads at most.

A delete is not made here. ``stat`` looks at the file (a regular file, in a
folder this session can write) and answers the three values that tell it
from every other: the platform's own delete owner removes the file only
when its own look shows the same three.

Six rules here are written a second time in kind, because this text can
load nothing of the platform: ``services/infra/safe_fs.py`` makes the same
kind of open, the same kind of walk (one folder held open a level, a folder
that could not be read told) and the same kind of write (a ``.partial``
file beside the name, moved over it) for the platform's own process, and
``files.py`` judges a path's row, a search's pattern and the place a link
lands before and after a call. One table for each pair runs against both
sides (``tests/execution/test_direct_file_tools.py``).
"""

import contextlib
import errno
import os
import posixpath
import re
import stat

#: A folder to list, never through a link.
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
#: A folder on the way, for search alone, never through a link: a folder
#: without the read bit is still passed, as the platform's own walk does.
_SEARCH = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
#: What every last open carries: no link followed, no wait on a pipe or a
#: device, never a controlling terminal.
_QUIET = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
#: The errors an open through a link at the name answers with.
_LINK_ERRNOS = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)
#: How many skills a listing reads at most.
_SKILLS_LISTED = 10_000
#: How many bytes of a name its temp file keeps, and how many random bytes
#: follow: the shape of the platform's own temp files (``safe_fs``), which
#: the sync passes over and retention removes.
_TEMP_NAME_BYTES = 200
_TEMP_TAG_BYTES = 6


class _No(Exception):
    """A refusal: its code, and what goes back with it."""

    def __init__(self, code, **more):
        super().__init__(code)
        self.answer = {"ok": False, "code": code, **more}


def _os_code(error):
    """The code of a failure of the file system."""
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        return "missing"
    if error.errno == errno.ELOOP:
        return "link"
    if error.errno in (errno.EDQUOT, errno.ENOSPC):
        return "no_space"
    if error.errno == errno.EROFS:
        return "read_only"
    if error.errno in (errno.EACCES, errno.EPERM):
        return "denied"
    return "os"


def _operation(run):
    """An operation whose refusals and file system failures are answers."""
    def answer(facts, args):
        try:
            return run(facts, args)
        except _No as no:
            return no.answer
        except OSError as e:
            return {"ok": False, "code": _os_code(e), "detail": e.strerror or str(e)}
    return answer


# -- The rules on a path ------------------------------------------------------

def _parts(path):
    return [part for part in path.split("/") if part]


def _within(path, folder):
    """Whether ``path`` is ``folder`` or below it, by whole parts."""
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def _row_of(facts, path):
    """The longest row that holds ``path``, None outside every row."""
    best = None
    for row in facts.get("rows") or ():
        if isinstance(row, str) and _within(path, row) and (best is None or len(row) > len(best)):
            best = row
    return best


def _refused_name(facts, path):
    """The first refused name on ``path``, "" when it has none."""
    refused = set(facts.get("refused") or ())
    return next((part for part in _parts(path) if part in refused), "")


def _admit(facts, path):
    """The row of a path the platform sent: absolute, already normal, with no
    refused name, inside a row."""
    if (not isinstance(path, str) or not path.startswith("/") or "\x00" in path
            or posixpath.normpath(path) != path):
        raise _No("bad_path")
    name = _refused_name(facts, path)
    if name:
        raise _No("refused_name", name=name)
    row = _row_of(facts, path)
    if row is None:
        raise _No("not_a_session_folder")
    return row


def _landing(facts, path, row, strict, last_as_given=False):
    """The path to open. One with no link in it is itself. One through a
    link is never opened here: where it lands stays in its row and off the
    refused names, and the answer names that place for the platform to judge.
    ``strict`` is the platform asking again for a place it judged. With
    ``last_as_given`` only the folder is followed and the last name stays as
    it was given: a delete removes the name it was given, never what a link
    there leads to."""
    if strict:
        return path
    if last_as_given:
        folder, name = posixpath.split(path)
        landed = posixpath.join(os.path.realpath(folder), name)
    else:
        landed = os.path.realpath(path)
    if landed == path:
        return path
    if not _within(landed, row):
        raise _No("leaves")
    if _refused_name(facts, landed):
        raise _No("elsewhere")
    raise _No("lands", landed=landed)


# -- Opens: from the root part by part, no link followed ----------------------

def _is_link(name, dir_fd):
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _step(name, flags, dir_fd):
    """Open ``name`` in the folder ``dir_fd``. A link at the name is a
    refusal, whatever it points at."""
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as e:
        if e.errno in _LINK_ERRNOS and _is_link(name, dir_fd):
            raise _No("link") from None
        raise


def _open_folder(path, last=_FOLDER, make=False):
    """A descriptor of the folder at ``path``, the caller's to close: from
    the root part by part, each part on the way for search alone, the last
    one opened with ``last`` (to list it, or for search alone), none through
    a link. With ``make`` a part that is missing is made and then opened the
    same way, so a link put there behind the ``mkdir`` is refused, not
    entered."""
    parts = _parts(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i, part in enumerate(parts):
            flags = last if i == len(parts) - 1 else _SEARCH
            try:
                nxt = _step(part, flags, fd)
            except FileNotFoundError:
                if not make:
                    raise
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o777, dir_fd=fd)
                nxt = _step(part, flags, fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_file(path):
    """A descriptor of the regular file at ``path``, open for reading. A
    folder, a pipe, a device or a socket at the name is a refusal, and none
    of them is waited on."""
    parent, name = posixpath.split(path)
    dir_fd = _open_folder(parent, _SEARCH)
    try:
        fd = _step(name, os.O_RDONLY | _QUIET, dir_fd)
    finally:
        os.close(dir_fd)
    mode = os.fstat(fd).st_mode
    if stat.S_ISREG(mode):
        return fd
    os.close(fd)
    raise _No("folder" if stat.S_ISDIR(mode) else "special")


def _read_capped(path, cap):
    """Up to ``cap`` bytes of the file at ``path`` and whether it holds more."""
    with os.fdopen(_open_file(path), "rb") as handle:
        data = handle.read(cap + 1)
    return data[:cap], len(data) > cap


# -- read ---------------------------------------------------------------------

@_operation
def _read(facts, args):
    path = args.get("path")
    row = _admit(facts, path)
    path = _landing(facts, path, row, bool(args.get("strict")))
    data, truncated = _read_capped(path, int(facts.get("read_max") or 0))
    if b"\x00" in data[:8192]:
        raise _No("binary")
    return {"ok": True, "text": data.decode("utf-8", "replace"), "truncated": truncated}


# -- glob ---------------------------------------------------------------------

def _segment_regex(part):
    """One pattern segment as a regular expression that never crosses a
    slash: Python's ``fnmatch`` translation, as ``glob.translate`` calls it."""
    star, any_one = "[^/]*", "[^/]"
    out = []
    i, n = 0, len(part)
    while i < n:
        c = part[i]
        i += 1
        if c == "*":
            if not out or out[-1] is not star:
                out.append(star)
        elif c == "?":
            out.append(any_one)
        elif c == "[":
            j = i
            if j < n and part[j] == "!":
                j += 1
            if j < n and part[j] == "]":
                j += 1
            while j < n and part[j] != "]":
                j += 1
            if j >= n:
                out.append("\\[")
                continue
            stuff = part[i:j]
            if "-" not in stuff:
                stuff = stuff.replace("\\", r"\\")
            else:
                chunks = []
                k = i + 2 if part[i] == "!" else i + 1
                while True:
                    k = part.find("-", k, j)
                    if k < 0:
                        break
                    chunks.append(part[i:k])
                    i = k + 1
                    k = k + 3
                chunk = part[i:j]
                if chunk:
                    chunks.append(chunk)
                else:
                    chunks[-1] += "-"
                for k in range(len(chunks) - 1, 0, -1):
                    if chunks[k - 1][-1] > chunks[k][0]:
                        chunks[k - 1] = chunks[k - 1][:-1] + chunks[k][1:]
                        del chunks[k]
                stuff = "-".join(s.replace("\\", r"\\").replace("-", r"\-") for s in chunks)
            stuff = re.sub(r"([&~|])", r"\\\1", stuff)
            i = j + 1
            if not stuff:
                out.append("(?!)")
            elif stuff == "!":
                out.append(".")
            else:
                if stuff[0] == "!":
                    stuff = "^" + stuff[1:]
                elif stuff[0] in ("^", "["):
                    stuff = "\\" + stuff
                out.append("[" + stuff + "]")
        else:
            out.append(re.escape(c))
    return out


def _glob_regex(pattern):
    """The regular expression of a glob pattern over names joined by a
    slash, where ``**`` is any number of folders: Python 3.13's
    ``glob.translate`` with hidden names included, which
    ``PurePosixPath.full_match`` reads and Python 3.10 does not have."""
    parts = [part for part in pattern.split("/") if part and part != "."]
    last = len(parts) - 1
    out = []
    for i, part in enumerate(parts):
        if part == "*":
            out.append("[^/]+/" if i < last else "[^/]+")
        elif part == "**":
            if i == last:
                out.append(".*")
            elif parts[i + 1] != "**":
                out.append("(?:.+/)?")
        else:
            out.extend(_segment_regex(part))
            if i < last:
                out.append("/")
    return "(?s:" + "".join(out) + r")\Z"


def _text_name(name):
    """Whether a name can be written back as text."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _lands_on_file(facts, path):
    """Whether the link at ``path`` lands on a regular file of its own row,
    off the refused names."""
    row = _row_of(facts, path)
    landed = os.path.realpath(path)
    if row is None or not _within(landed, row) or _refused_name(facts, landed):
        return False
    try:
        return stat.S_ISREG(os.stat(landed, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _walk(facts, top, base, matches, found, cap):
    """The names below the open folder ``top`` (at ``base``) that match, into
    ``found``, a folder's own names first, until ``cap`` names are found. No
    link is entered, nor a skipped name. A link is listed only where it
    lands on a regular file. One folder is held open a level, so a wide tree
    cannot run the program out of open files.

    Returns how many folders it could not read, so that a search with a gap
    never reads as whole: a folder whose listing failed (what it listed
    before stays) and a folder that could not be entered. One that is gone
    since its listing, or a link by now, is no gap: nothing is there to
    search, and no link is entered."""
    skip = set(facts.get("glob_skip") or ())
    unread = 0
    # One entry a level: a folder's descriptor, its path below top, and its
    # subfolders still to enter (None until it is listed). A folder is here
    # from the moment it is opened, so the end closes every one.
    levels = [(top, "", None)]
    try:
        while levels:
            fd, here, folders = levels[-1]
            if folders is None:
                names = []
                try:
                    with os.scandir(fd) as listing:
                        for entry in sorted(listing, key=lambda entry: entry.name):
                            name = entry.name
                            if not _text_name(name):
                                continue
                            rel = here + "/" + name if here else name
                            if entry.is_symlink():
                                if matches(rel) and _lands_on_file(facts, posixpath.join(base, rel)):
                                    found.append(rel)
                            elif entry.is_dir(follow_symlinks=False):
                                if name not in skip:
                                    names.append(name)
                            elif entry.is_file(follow_symlinks=False) and matches(rel):
                                found.append(rel)
                            if len(found) >= cap:
                                return unread
                except OSError:
                    unread += 1
                levels[-1] = (fd, here, iter(names))
                continue
            name = next(folders, None)
            if name is None:
                levels.pop()
                if fd != top:
                    os.close(fd)
                continue
            try:
                child = _step(name, _FOLDER, fd)
            except _No:
                continue                                  # a link by now
            except OSError as e:
                if e.errno != errno.ENOENT:               # gone since its listing: no gap
                    unread += 1
                continue
            levels.append((child, here + "/" + name if here else name, None))
        return unread
    finally:
        for fd, _here, _folders in levels:
            if fd != top:
                os.close(fd)


@_operation
def _glob(facts, args):
    path = args.get("path")
    row = _admit(facts, path)
    path = _landing(facts, path, row, bool(args.get("strict")))
    pattern = args.get("pattern")
    pattern = pattern.strip() if isinstance(pattern, str) else ""
    if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
        raise _No("bad_pattern")
    matches = re.compile(_glob_regex(pattern)).match
    try:
        top = _open_folder(path)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            raise _No("not_a_folder") from None
        raise
    found = []
    try:
        unread = _walk(facts, top, path, matches, found, int(facts.get("glob_max") or 0))
    finally:
        os.close(top)
    answer = {"ok": True, "rels": sorted(found)}
    if unread:
        # The search has a gap, and its answer says so.
        answer["unread"] = unread
    return answer


# -- The session's skills: one fixed folder of the facts, never a path --------

@_operation
def _skills(facts, args):
    root = facts.get("skills")
    if not isinstance(root, str) or not root:
        return {"ok": True, "names": []}
    try:
        top = _open_folder(root)
    except (_No, OSError):
        return {"ok": True, "names": []}
    names = []
    try:
        with os.scandir(top) as listing:
            for entry in listing:
                if len(names) >= _SKILLS_LISTED:
                    break
                name = entry.name
                if name.startswith(".") or not _text_name(name):
                    continue
                try:
                    folder = _step(name, _FOLDER, top)   # a folder itself, never a link to one
                except (_No, OSError):
                    continue
                try:
                    mode = os.stat("SKILL.md", dir_fd=folder, follow_symlinks=False).st_mode
                except OSError:
                    continue
                finally:
                    os.close(folder)
                if stat.S_ISREG(mode):
                    names.append(name)
    finally:
        os.close(top)
    return {"ok": True, "names": sorted(names)}


@_operation
def _skill(facts, args):
    root = facts.get("skills")
    name = args.get("name")
    if (not isinstance(root, str) or not root or not isinstance(name, str) or not name
            or "/" in name or "\x00" in name or name in (".", "..")):
        raise _No("missing")
    data, more = _read_capped(posixpath.join(root, name, "SKILL.md"), int(facts.get("skill_max") or 0))
    if more:
        raise _No("too_large")
    return {"ok": True, "text": data.decode("utf-8", "replace")}


# -- write, edit and the delete's look ----------------------------------------

def _facts_of(status):
    """What tells one file from every other, and from itself once changed:
    its size, its inode number and its change time."""
    return [status.st_size, status.st_ino, status.st_ctime_ns]


def _temp_name(name):
    """The name of the temp file beside ``name``."""
    base = os.fsdecode(os.fsencode(name)[:_TEMP_NAME_BYTES])
    return "." + base + "." + os.urandom(_TEMP_TAG_BYTES).hex() + ".partial"


def _open_existing(name, folder, access=os.O_WRONLY):
    """The file at ``name`` of the open folder, opened for writing with no
    link followed, without waiting and with nothing cut: the kernel's own
    word on whether this session may write that file. None when the name is
    free. A folder, a link and whatever is no regular file are refusals."""
    try:
        fd = os.open(name, access | _QUIET, dir_fd=folder)
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise _No("folder") from None
    except OSError as e:
        if e.errno in _LINK_ERRNOS and _is_link(name, folder):
            raise _No("link_at_name") from None
        if e.errno == errno.ENXIO:                     # a pipe nobody reads, a socket
            raise _No("special") from None
        raise
    if stat.S_ISREG(os.fstat(fd).st_mode):
        return fd
    os.close(fd)
    raise _No("special")


def _write_whole(folder, name, payload, existing, still=None):
    """``payload`` as the file ``name`` of the open folder, whole or not
    there: written into a temp file beside the name (created with no link
    followed, refused when it exists) and moved over the name in the same
    folder. A reader sees the old file or the new one, never half of one,
    also when this program is ended in the middle. The new file takes the
    bits of the one it replaces (``existing``, its descriptor), a new name
    the umask's. With ``still`` the name must show those three values right
    before the move, or nothing is written: an edit does not replace a file
    that this look shows as another one. The look cannot see a change of the
    same size inside one step of the change time, nor one between itself and
    the move."""
    temp = _temp_name(name)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o666 if existing is None else 0o600, dir_fd=folder)
    moved = False
    try:
        with os.fdopen(fd, "wb") as handle:
            if existing is not None:
                os.fchmod(handle.fileno(), stat.S_IMODE(os.fstat(existing).st_mode))
            handle.write(payload)
        if still is not None:
            try:
                now = _facts_of(os.stat(name, dir_fd=folder, follow_symlinks=False))
            except FileNotFoundError:
                now = None
            if now != still:
                raise _No("changed")
        os.rename(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
        moved = True
    finally:
        if not moved:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=folder)


def _as_bytes(text):
    """A call's text as the bytes a file holds."""
    if not isinstance(text, str):
        raise _No("bad_content")
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise _No("bad_content") from None


@_operation
def _write(facts, args):
    path = args.get("path")
    _admit(facts, path)
    payload = _as_bytes(args.get("content"))
    if len(payload) > int(facts.get("write_max") or 0):
        raise _No("too_large")
    parent, name = posixpath.split(path)
    try:
        folder = _open_folder(parent, _SEARCH, make=True)
    except NotADirectoryError as e:
        raise _No("os", detail=e.strerror) from None   # a file where a folder is needed
    try:
        existing = _open_existing(name, folder)
        try:
            _write_whole(folder, name, payload, existing)
        finally:
            if existing is not None:
                os.close(existing)
    finally:
        os.close(folder)
    return {"ok": True, "bytes": len(payload), "created": existing is None}


def _read_all(fd, cap):
    """Up to ``cap`` bytes from the start of the open file ``fd``."""
    chunks, left = [], cap
    while left > 0:
        chunk = os.pread(fd, min(left, 1 << 16), cap - left)
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


@_operation
def _edit(facts, args):
    path = args.get("path")
    _admit(facts, path)
    old, new = _as_bytes(args.get("old")), _as_bytes(args.get("new"))
    if not old:
        raise _No("bad_content")
    cap = int(facts.get("write_max") or 0)
    parent, name = posixpath.split(path)
    folder = _open_folder(parent, _SEARCH)
    try:
        # Read and write through one descriptor: the kernel's word on both,
        # and the file that is read is the file that was opened.
        fd = _open_existing(name, folder, os.O_RDWR)
        if fd is None:
            raise _No("missing")
        try:
            before = _facts_of(os.fstat(fd))
            data = _read_all(fd, cap + 1)
            if len(data) > cap:
                raise _No("too_large")
            if b"\x00" in data[:8192]:
                raise _No("binary")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise _No("not_text") from None
            found = data.count(old)
            if found == 0:
                raise _No("not_found_text")
            every = bool(args.get("replace_all"))
            if found > 1 and not every:
                raise _No("many", count=found)
            count = found if every else 1
            # Judged before the result is built: an edit never makes a file
            # that the next edit could not read.
            if len(data) + count * (len(new) - len(old)) > cap:
                raise _No("grows")
            # Every place is replaced: with more than one, ``replace_all`` was asked for.
            _write_whole(folder, name, data.replace(old, new), fd, still=before)
        finally:
            os.close(fd)
    finally:
        os.close(folder)
    return {"ok": True, "count": count}


@_operation
def _stat(facts, args):
    path = args.get("path")
    row = _admit(facts, path)
    path = _landing(facts, path, row, bool(args.get("strict")), last_as_given=True)
    parent, name = posixpath.split(path)
    folder = _open_folder(parent, _SEARCH)
    try:
        status = os.stat(name, dir_fd=folder, follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode):
            raise _No("folder")
        if not stat.S_ISREG(status.st_mode):
            raise _No("special")
        # Whether this session may remove a name here is the kernel's word:
        # a read-only mount and a folder without the write bit both say no.
        if not os.access(".", os.W_OK | os.X_OK, dir_fd=folder):
            raise _No("read_only")
    finally:
        os.close(folder)
    size, inode, changed = _facts_of(status)
    return {"ok": True, "size": size, "inode": inode, "changed": changed}


#: The program's table: what the loop calls by name.
OPERATIONS = {"read": _read, "glob": _glob, "skill": _skill, "skills": _skills,
              "write": _write, "edit": _edit, "stat": _stat}
