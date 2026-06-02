#!/usr/bin/env python3
"""Recursively resolve and download a PyPI package and all of its dependencies.

Given a package name on the command line, this script queries the PyPI JSON API
to discover the package's dependencies (``requires_dist``), then repeats the
process for every dependency, walking the whole tree breadth-first while
avoiding cycles. Once the tree is resolved it (optionally) downloads the
distribution files for every package into a local directory.

Examples
--------
    # Resolve + download httpx and everything it needs into ./downloads
    python PyDD.py httpx

    # Include an optional feature set (extra) and only fetch wheels
    python PyDD.py httpx --extras http2 --wheels-only

    # Download wheels for a *different* platform than the current machine
    python PyDD.py cryptography --platform windows
    python PyDD.py cryptography --platform win_amd64
    python PyDD.py cryptography --platform manylinux2014_aarch64

    # Narrow to a single platform + Python version (64-bit Windows, CPython 3.12)
    python PyDD.py cryptography --platform win_amd64 --python-version 3.12

    # Resolve every package listed in a requirements.txt file (recursively)
    python PyDD.py -r requirements.txt

    # Just print the dependency tree, don't download anything
    python PyDD.py httpx --no-download

Author
------
Ray Carioto <raymond.carioto@gmail.com>

License
-------
Copyright (C) 2026 Ray Carioto <raymond.carioto@gmail.com>

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "Ray Carioto <raymond.carioto@gmail.com>"
__license__ = "GPL-3.0-or-later"
__copyright__ = "Copyright (C) 2026 Ray Carioto"

import argparse
import platform as platform_module
import re
import sys
from collections import deque
from pathlib import Path
from typing import Iterable

import requests

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
REQUEST_TIMEOUT = 30

# Prefer the real PEP 508 parser when it is installed; otherwise fall back to a
# best-effort parser defined further below. ``packaging`` gives us accurate
# environment-marker evaluation (python_version, sys_platform, extras, ...).
try:
    from packaging.requirements import Requirement  # type: ignore
    from packaging.utils import canonicalize_name  # type: ignore

    HAVE_PACKAGING = True
except ImportError:  # pragma: no cover - exercised only without packaging
    HAVE_PACKAGING = False

    def canonicalize_name(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()


class DependencyResolver:
    """Walks the PyPI dependency graph for a set of root packages."""

    def __init__(self, extras: Iterable[str] | None = None, verbose: bool = True):
        # Extras requested for the *root* packages (e.g. httpx[http2]).
        self.requested_extras = {e.strip() for e in (extras or []) if e.strip()}
        self.verbose = verbose
        # Maps canonical name -> the PyPI JSON payload we fetched for it.
        self.metadata: dict[str, dict] = {}
        # Canonical name -> set of canonical dependency names.
        self.edges: dict[str, set[str]] = {}
        self.failed: dict[str, str] = {}

    def log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def fetch_metadata(self, name: str) -> dict | None:
        """Fetch and cache the PyPI JSON metadata for ``name``."""
        canon = canonicalize_name(name)
        if canon in self.metadata:
            return self.metadata[canon]
        try:
            response = requests.get(
                PYPI_JSON_URL.format(name=name), timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            self.failed[canon] = str(exc)
            self.log(f"  ! failed to fetch {name!r}: {exc}")
            return None
        self.metadata[canon] = data
        return data

    def dependencies_for(self, data: dict, *, is_root: bool) -> set[str]:
        """Extract runtime dependency names from a package's metadata.

        Extras are only expanded for root packages (the things the user asked
        for directly); transitive packages contribute their unconditional
        runtime requirements.
        """
        requires = data["info"].get("requires_dist") or []
        extras = self.requested_extras if is_root else set()
        deps: set[str] = set()
        for raw in requires:
            parsed = parse_requirement(raw, extras=extras)
            if parsed is not None:
                deps.add(parsed)
        return deps

    def resolve(self, roots: Iterable[str]) -> None:
        """Breadth-first walk of the dependency graph starting at ``roots``."""
        queue: deque[tuple[str, bool]] = deque((r, True) for r in roots)
        seen: set[str] = set()
        while queue:
            name, is_root = queue.popleft()
            canon = canonicalize_name(name)
            if canon in seen:
                continue
            seen.add(canon)

            self.log(f"Resolving {name} ...")
            data = self.fetch_metadata(name)
            if data is None:
                continue

            deps = self.dependencies_for(data, is_root=is_root)
            self.edges[canon] = {canonicalize_name(d) for d in deps}
            for dep in sorted(deps):
                self.log(f"  -> {dep}")
                if canonicalize_name(dep) not in seen:
                    queue.append((dep, False))


def parse_requirement(raw: str, extras: set[str]) -> str | None:
    """Return the dependency's package name, or ``None`` if it doesn't apply.

    ``raw`` is a PEP 508 requirement string such as
    ``"h2 (>=3,<5) ; extra == 'http2'"``. We honour environment markers so we
    only follow dependencies that are actually relevant for this interpreter
    and for the requested extras.
    """
    if HAVE_PACKAGING:
        return _parse_with_packaging(raw, extras)
    return _parse_fallback(raw, extras)


def _parse_with_packaging(raw: str, extras: set[str]) -> str | None:
    try:
        req = Requirement(raw)
    except Exception:
        return None
    if req.marker is not None:
        # Evaluate the marker against the current environment. If the
        # requirement is gated behind an extra, test each requested extra.
        if extras:
            if not any(req.marker.evaluate({"extra": e}) for e in extras):
                # Could still be a non-extra marker that passes on its own.
                if not req.marker.evaluate():
                    return None
        elif not req.marker.evaluate():
            return None
    return req.name


# --- Fallback parser (used only when ``packaging`` is unavailable) -----------

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_RE = re.compile(r"extra\s*==\s*['\"]([^'\"]+)['\"]")
_PYVER_RE = re.compile(
    r"python_version\s*(==|!=|<=|>=|<|>)\s*['\"]([^'\"]+)['\"]"
)


def _parse_fallback(raw: str, extras: set[str]) -> str | None:
    if not raw or not raw.strip():
        return None
    name_part, _, marker = raw.partition(";")
    match = _NAME_RE.match(name_part)
    if not match:
        return None
    name = match.group(1)

    marker = marker.strip()
    if not marker:
        return name

    # Handle "extra == '...'" gates.
    extra_names = _EXTRA_RE.findall(marker)
    if extra_names and not (set(extra_names) & extras):
        return None

    # Best-effort python_version evaluation against the running interpreter.
    for op, version in _PYVER_RE.findall(marker):
        if not _compare_python_version(op, version):
            return None

    return name


def _compare_python_version(op: str, version: str) -> bool:
    current = sys.version_info[:2]
    target = tuple(int(p) for p in re.findall(r"\d+", version)[:2])
    if not target:
        return True
    target = target + (0,) * (2 - len(target))
    ops = {
        "==": current == target,
        "!=": current != target,
        "<=": current <= target,
        ">=": current >= target,
        "<": current < target,
        ">": current > target,
    }
    return ops.get(op, True)


# --- Platform / architecture selection ---------------------------------------

# Maps a friendly OS name to substrings that appear in wheel platform tags.
# e.g. a Linux wheel tag looks like ``manylinux2014_x86_64`` or
# ``musllinux_1_1_aarch64``; a macOS one like ``macosx_11_0_arm64``; a Windows
# one like ``win_amd64`` / ``win32`` / ``win_arm64``.
OS_ALIASES: dict[str, tuple[str, ...]] = {
    "linux": ("linux",),
    "macos": ("macosx",),
    "osx": ("macosx",),
    "mac": ("macosx",),
    "darwin": ("macosx",),
    "windows": ("win",),
    "win": ("win",),
}


def detect_current_platform() -> str:
    """Return a friendly OS name for the machine we are running on."""
    system = platform_module.system().lower()
    return {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(
        system, system
    )


def platform_matchers(platform_spec: str) -> tuple[str, ...]:
    """Turn a ``--platform`` value into platform-tag substrings to match.

    A high-level OS name (``linux``/``macos``/``windows``) expands to that
    family's tag substrings. Anything else is treated as a literal platform-tag
    substring, so values like ``win_amd64``, ``manylinux2014_aarch64`` or
    ``macosx_11_0_arm64`` work as-is.
    """
    spec = platform_spec.strip().lower()
    return OS_ALIASES.get(spec, (spec,))


def wheel_platform_tags(filename: str) -> list[str]:
    """Extract the platform tag(s) from a wheel filename.

    Wheel names look like ``name-version(-build)?-pytag-abitag-platformtag.whl``
    and the final field may itself be a ``.``-joined set of platform tags
    (e.g. ``manylinux1_x86_64.manylinux2010_x86_64``).
    """
    if not filename.endswith(".whl"):
        return []
    parts = filename[:-4].split("-")
    if len(parts) < 4:
        return []
    return parts[-1].split(".")


def file_matches_platform(file_info: dict, matchers: tuple[str, ...]) -> bool:
    """Return True if a distribution file is usable on the target platform."""
    # Source distributions are platform independent (they build anywhere).
    if file_info.get("packagetype") == "sdist":
        return True
    tags = wheel_platform_tags(file_info.get("filename", ""))
    if not tags:
        return False
    # ``any`` => pure-Python wheel, valid on every platform.
    if "any" in tags:
        return True
    return any(m in tag for tag in tags for m in matchers)


def wheel_python_tags(filename: str) -> list[str]:
    """Extract the Python tag(s) from a wheel filename.

    The Python tag is the third field from the end (``...-pytag-abitag-plat.whl``)
    and may be a ``.``-joined set, e.g. ``py2.py3`` or ``cp39.cp310``.
    """
    if not filename.endswith(".whl"):
        return []
    parts = filename[:-4].split("-")
    if len(parts) < 4:
        return []
    return parts[-3].split(".")


# A free-threaded (no-GIL) CPython wheel carries an ABI tag like ``cp313t``;
# the standard GIL build uses ``cp313`` (no trailing ``t``). Tags such as
# ``abi3`` / ``none`` are version-agnostic and run on either interpreter.
_FREE_THREADED_ABI_RE = re.compile(r"^cp\d+t$")
_STANDARD_CP_ABI_RE = re.compile(r"^cp\d+$")


def wheel_abi_tags(filename: str) -> list[str]:
    """Extract the ABI tag(s) from a wheel filename (second field from the end)."""
    if not filename.endswith(".whl"):
        return []
    parts = filename[:-4].split("-")
    if len(parts) < 4:
        return []
    return parts[-2].split(".")


def file_matches_abi(file_info: dict, free_threaded: bool) -> bool:
    """Filter wheels by interpreter build (standard GIL vs free-threaded).

    ABI-agnostic wheels (``abi3``/``none``) and source distributions install on
    either interpreter and are always kept. Version-specific ``cpXXX`` wheels
    are GIL-only; ``cpXXXt`` wheels are free-threaded-only.
    """
    if file_info.get("packagetype") == "sdist":
        return True
    abis = wheel_abi_tags(file_info.get("filename", ""))
    if not abis:
        return False
    is_free_threaded = any(_FREE_THREADED_ABI_RE.match(a) for a in abis)
    is_standard_specific = any(_STANDARD_CP_ABI_RE.match(a) for a in abis)
    if free_threaded:
        # Want the no-GIL build: drop GIL-only standard wheels.
        return not (is_standard_specific and not is_free_threaded)
    # Default: standard build, drop free-threaded wheels.
    return not is_free_threaded


def _version_pair(text: str) -> tuple[int, int] | None:
    """Parse a (major, minor) version from a spec or tag.

    Accepts dotted forms (``3.12``, ``3.13.7``) and tag/compact forms
    (``cp312``, ``312``, ``cp39``). For compact forms the first digit is the
    major version and the remaining digits are the minor version.
    """
    text = text.strip().lower()
    if "." in text:
        nums = re.findall(r"\d+", text)
    else:
        digits = re.sub(r"\D", "", text)
        if not digits:
            return None
        nums = [digits[0], digits[1:]]
    if not nums or not nums[0]:
        return None
    minor = int(nums[1]) if len(nums) > 1 and nums[1] else 0
    return int(nums[0]), minor


def file_matches_python(file_info: dict, python_spec: str) -> bool:
    """Return True if a distribution file targets the requested Python version.

    ``python_spec`` is flexible: ``3.12``, ``312``, or ``cp312`` all select the
    same interpreter. Source distributions and version-agnostic ``py2``/``py3``
    wheels are always eligible (they work across CPython versions).

    Stable-ABI wheels (``abi3``) carry a Python tag that denotes the *minimum*
    supported version, e.g. ``cp310-abi3`` runs on CPython 3.10 and newer, so a
    request for 3.12 must match it.
    """
    if not python_spec:
        return True
    if file_info.get("packagetype") == "sdist":
        return True
    filename = file_info.get("filename", "")
    tags = wheel_python_tags(filename)
    if not tags:
        return False
    requested = _version_pair(python_spec)
    if requested is None:  # unparseable spec -> don't filter it out
        return True
    is_abi3 = "abi3" in wheel_abi_tags(filename)
    for tag in tags:
        # ``py3`` / ``py2.py3`` => generic, works on any matching-major CPython.
        if tag.startswith("py"):
            return True
        tag_version = _version_pair(tag)
        if tag_version is None:
            continue
        if tag_version == requested:
            return True
        # abi3 tags are a lower bound: cp310-abi3 supports 3.10+.
        if (
            is_abi3
            and tag_version[0] == requested[0]
            and requested[1] >= tag_version[1]
        ):
            return True
    return False


# --- Downloading -------------------------------------------------------------

def download_packages(
    resolver: DependencyResolver,
    dest: Path,
    *,
    wheels_only: bool = False,
    target_platform: str = "",
    python_version: str = "",
    free_threaded: bool = False,
) -> None:
    """Download distribution files for every resolved package.

    ``target_platform`` filters wheels to those compatible with the chosen
    platform (defaults to the current machine when empty). ``python_version``
    (e.g. ``3.12``/``312``/``cp312``) restricts wheels to that interpreter.
    ``free_threaded`` selects no-GIL (``cpXXXt``) wheels instead of the standard
    GIL build. Pure-Python wheels and source distributions are always eligible.
    """
    matchers = platform_matchers(target_platform or detect_current_platform())
    dest.mkdir(parents=True, exist_ok=True)
    total_files = 0
    for canon, data in sorted(resolver.metadata.items()):
        info = data["info"]
        version = info.get("version", "")
        # ``urls`` holds the files for the latest release returned by the API.
        files = data.get("urls") or []
        if wheels_only:
            files = [f for f in files if f.get("packagetype") == "bdist_wheel"]
        files = [f for f in files if file_matches_platform(f, matchers)]
        files = [f for f in files if file_matches_python(f, python_version)]
        files = [f for f in files if file_matches_abi(f, free_threaded)]
        if not files:
            details = f"platform {target_platform or detect_current_platform()!r}"
            if python_version:
                details += f", python {python_version!r}"
            print(f"  ! no downloadable files for {canon} ({version}) matching {details}")
            continue
        for file_info in files:
            filename = file_info["filename"]
            target = dest / filename
            if target.exists():
                print(f"  = already have {filename}")
                continue
            print(f"  + downloading {filename}")
            try:
                _download_file(file_info["url"], target)
                total_files += 1
            except requests.RequestException as exc:
                print(f"  ! failed to download {filename}: {exc}")
    print(f"\nDownloaded {total_files} file(s) into {dest}")


def ensure_directory(raw_path: str) -> Path:
    """Resolve ``raw_path`` (expanding ``~``) and create it if it doesn't exist.

    Returns the absolute path to a usable directory. Raises ``NotADirectoryError``
    if the path already exists as a file, or ``OSError`` if it can't be created.
    """
    path = Path(raw_path).expanduser()
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"{path} exists and is not a directory")
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    path = path.resolve()
    print(f"{'Created' if created else 'Using'} download directory: {path}")
    return path


def _download_file(url: str, target: Path) -> None:
    with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        tmp = target.with_suffix(target.suffix + ".part")
        with open(tmp, "wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                handle.write(chunk)
        tmp.replace(target)


# --- CLI ---------------------------------------------------------------------

def print_tree(resolver: DependencyResolver, roots: list[str]) -> None:
    print("\nDependency tree")
    print("===============")
    visited: set[str] = set()

    def walk(name: str, depth: int) -> None:
        canon = canonicalize_name(name)
        indent = "  " * depth
        marker = ""
        if canon in visited:
            print(f"{indent}{name} (already shown)")
            return
        visited.add(canon)
        if canon in resolver.failed:
            marker = "  [unresolved]"
        print(f"{indent}{name}{marker}")
        for dep in sorted(resolver.edges.get(canon, set())):
            walk(dep, depth + 1)

    for root in roots:
        walk(root, 0)


# --- requirements.txt parsing ------------------------------------------------

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")


def _parse_req_line(line: str) -> tuple[str | None, set[str]]:
    """Return ``(name, extras)`` for a single requirement line."""
    if HAVE_PACKAGING:
        try:
            req = Requirement(line)
            return req.name, set(req.extras)
        except Exception:
            pass
    match = _REQ_NAME_RE.match(line)
    if not match:
        return None, set()
    extras = {e.strip() for e in (match.group(2) or "").split(",") if e.strip()}
    return match.group(1), extras


def parse_requirements_file(
    path: Path, _seen: set[Path] | None = None
) -> tuple[list[str], set[str]]:
    """Read package names (and extras) from a requirements.txt-style file.

    Handles comments, blank lines, line continuations, ``-r``/``--requirement``
    includes (resolved relative to the including file), and ``pkg[extra]``
    syntax. Other pip options (``-e``, ``--index-url``, ``--hash``, ...) are
    skipped. Returns the discovered package names and the union of extras.
    """
    _seen = set() if _seen is None else _seen
    resolved = path.expanduser().resolve()
    if resolved in _seen:  # guard against circular -r includes
        return [], set()
    _seen.add(resolved)

    text = resolved.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\\n", " ")  # honour line continuations

    names: list[str] = []
    extras: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement")):
            included = line.split(None, 1)[1].strip()
            sub_names, sub_extras = parse_requirements_file(
                resolved.parent / included, _seen
            )
            names.extend(sub_names)
            extras |= sub_extras
            continue
        if line.startswith("-"):  # skip -e, --index-url, --hash, etc.
            continue
        name, line_extras = _parse_req_line(line)
        if name:
            names.append(name)
            extras |= line_extras
    return names, extras


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recursively resolve and download a PyPI package's dependencies."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "packages",
        nargs="*",
        help="package name(s) to resolve (optional if -r/--requirements is given)",
    )
    parser.add_argument(
        "-r",
        "--requirements",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "read package names from a requirements.txt-style file and resolve "
            "them recursively (may be repeated). Supports comments, pkg[extra] "
            "syntax, and nested -r includes."
        ),
    )
    parser.add_argument(
        "-d",
        "--dest",
        "-o",
        "--output",
        dest="dest",
        default="downloads",
        help=(
            "directory to download distribution files into; created if it does "
            "not exist (default: ./downloads). '~' is expanded."
        ),
    )
    parser.add_argument(
        "--extras",
        default="",
        help="comma-separated extras to include for the root package(s), e.g. 'http2,cli'",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="only resolve and print the dependency tree; do not download files",
    )
    parser.add_argument(
        "--wheels-only",
        action="store_true",
        help="download only wheel (.whl) files, skip source distributions",
    )
    parser.add_argument(
        "--platform",
        default="",
        help=(
            "target platform for wheels: a family name (linux, macos, windows) "
            "or a literal wheel platform tag (e.g. win_amd64, manylinux2014_aarch64, "
            "macosx_11_0_arm64). Defaults to the current machine. Pure-Python "
            "wheels and source distributions are always included."
        ),
    )
    parser.add_argument(
        "--python-version",
        default="",
        help=(
            "restrict wheels to a Python version, e.g. '3.13', '3.13.7', '313' "
            "or 'cp313' (the patch level is ignored since wheels use major.minor). "
            "Source distributions and version-agnostic (py3) wheels are always "
            "included. Defaults to all versions."
        ),
    )
    parser.add_argument(
        "--free-threaded",
        action="store_true",
        help=(
            "download free-threaded (no-GIL, cpXXXt) wheels instead of the "
            "standard GIL build. By default only standard-build wheels are "
            "fetched; ABI-agnostic (abi3/py3) wheels are kept either way."
        ),
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the per-package resolution log",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    extras = [e for e in args.extras.split(",") if e.strip()]

    if not HAVE_PACKAGING:
        print(
            "note: 'packaging' is not installed; using a best-effort requirement "
            "parser. Install it (pip install packaging) for accurate results.\n"
        )

    # Gather root packages from the command line and any requirements files.
    roots: list[str] = list(args.packages)
    for req_file in args.requirements:
        try:
            file_names, file_extras = parse_requirements_file(Path(req_file))
        except OSError as exc:
            print(f"error: could not read requirements file {req_file!r}: {exc}")
            return 2
        print(f"Read {len(file_names)} package(s) from {req_file}")
        roots.extend(file_names)
        extras.extend(e for e in file_extras if e not in extras)

    # De-duplicate roots while preserving order.
    roots = list(dict.fromkeys(roots))
    if not roots:
        print("error: no packages given; provide package name(s) or -r/--requirements FILE")
        return 2

    resolver = DependencyResolver(extras=extras, verbose=not args.quiet)
    resolver.resolve(roots)

    print_tree(resolver, roots)

    resolved = sorted(resolver.metadata)
    print(f"\nResolved {len(resolved)} package(s): {', '.join(resolved)}")
    if resolver.failed:
        print(f"Could not resolve {len(resolver.failed)} package(s): "
              f"{', '.join(sorted(resolver.failed))}")

    if not args.no_download:
        target_platform = args.platform or detect_current_platform()
        try:
            dest = ensure_directory(args.dest)
        except OSError as exc:
            print(f"error: could not prepare download directory: {exc}")
            return 2
        print(f"\nDownloading into {dest} (platform: {target_platform}) ...")
        download_packages(
            resolver,
            dest,
            wheels_only=args.wheels_only,
            target_platform=args.platform,
            python_version=args.python_version,
            free_threaded=args.free_threaded,
        )

    return 1 if resolver.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
