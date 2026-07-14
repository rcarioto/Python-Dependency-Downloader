# Python-Dependency-Downloader

A small command-line utility that, given one or more package names (or a
`requirements.txt` file), **recursively resolves the full PyPI dependency tree**
and downloads the distribution files (wheels / source archives) for every
package into a local directory.

It talks directly to the [PyPI JSON API](https://warehouse.pypa.io/api-reference/json.html),
walks each package's `requires_dist` metadata breadth-first, and can target a
**specific platform, Python version, and interpreter build** — even one that
differs from the machine you're running on. This makes it handy for building an
offline wheel cache or staging dependencies for an air-gapped/locked-down host.

## Features

- Recursive dependency resolution (handles shared deps and cycles).
- Correct PEP 508 requirement & environment-marker parsing (via `packaging`),
  with a built-in best-effort fallback when `packaging` isn't installed.
- Optional **extras** support (e.g. `httpx[http2]`), including extras requested
  on **transitive** dependencies (e.g. `mcp` -> `pyjwt[crypto]` -> `cryptography`).
- **Version pinning** via pip-style specifiers (e.g. `pkg==1.2.3`, `pkg>=13,<14`)
  for the packages you request directly.
- **Transitive exact pins** are honoured: when a package's metadata requires a
  dependency with an exact `==` version, that version is fetched; other
  specifiers (`>=`, ranges, none) use the latest release.
- Cross-platform downloads: pick `linux`, `macos`, `windows`, or a literal wheel
  platform tag (`win_amd64`, `manylinux2014_aarch64`, `macosx_11_0_arm64`, ...).
- Python-version filtering (`3.13`, `3.13.7`, `cp313`), including correct
  handling of **stable-ABI (`abi3`) wheels**, whose tag is a *minimum* version.
- Standard (GIL) vs **free-threaded** (no-GIL, `cpXXXt`) wheel selection.
- Read package lists from one or more `requirements.txt` files (with comment,
  `pkg[extra]`, and nested `-r` include support).
- Prints a readable dependency tree and downloads only the files you need.

## Requirements

- Python 3.8+
- [`requests`](https://pypi.org/project/requests/) (required)
- [`packaging`](https://pypi.org/project/packaging/) (recommended — enables
  accurate requirement/marker parsing; the script still runs without it)

Install the dependencies with:

```bash
pip install -r requirements.txt
```

## Usage

```bash
python PyDD.py [PACKAGES ...] [options]
```

You must provide at least one package name or a `-r/--requirements` file.

### Options

| Option | Description |
|---|---|
| `packages` | One or more package names to resolve (optional if `-r` is given). |
| `-r`, `--requirements FILE` | Read package names from a requirements.txt-style file. May be repeated. |
| `-d`, `--dest`, `-o`, `--output DIR` | Download directory; created if missing. `~` is expanded. Default: `./downloads`. |
| `-e`, `--extras LIST` | Comma-separated extras for the root package(s), e.g. `http2,cli`. |
| `-p`, `--platform SPEC` | Target platform: `linux` / `macos` / `windows`, or a literal wheel platform tag. Defaults to the current machine. |
| `-P`, `--python-version VER` | Restrict wheels to a Python version (`3.13`, `3.13.7`, `313`, `cp313`). Patch level is ignored. |
| `-f`, `--free-threaded` | Download free-threaded (no-GIL, `cpXXXt`) wheels instead of the standard GIL build. |
| `-w`, `--wheels-only` | Download only wheels (`.whl`); skip source distributions. |
| `-n`, `--no-download` | Only resolve and print the dependency tree; download nothing. |
| `-q`, `--quiet` | Suppress the per-package resolution log. |

## Examples

Resolve and download `httpx` plus everything it needs into `./downloads`:

```bash
python PyDD.py httpx
```

Just inspect the dependency tree without downloading:

```bash
python PyDD.py httpx --no-download
```

Include an optional feature set (extra) and fetch wheels only:

```bash
python PyDD.py httpx --extras http2 --wheels-only
```

Download Windows 64-bit wheels for CPython 3.13 while sitting on a different OS:

```bash
python PyDD.py PySide6 --platform win_amd64 --python-version 3.13.7 --wheels-only -o ~/pyside6_wheels
```

Download a specific version instead of the latest release:

```bash
python PyDD.py pydantic-core==2.46.4
python PyDD.py "rich>=13,<14"
```

Resolve everything listed in a requirements file (recursively):

```bash
python PyDD.py -r requirements.txt --platform manylinux2014_aarch64 --python-version 3.11
```

Get the free-threaded (no-GIL) build instead of the standard one:

```bash
python PyDD.py numpy --python-version 3.13 --free-threaded
```

## How filtering works

Each candidate distribution file is kept only if it passes every active filter:

- **Platform** — a family name (`linux`/`macos`/`windows`) expands to the tag
  substrings for that OS; anything else is matched as a literal platform-tag
  substring. Pure-Python wheels (`...-none-any.whl`) and source distributions
  are always eligible.
- **Python version** — version-specific wheels (`cp312-cp312`) must match
  exactly. Stable-ABI wheels (`cp310-abi3`) are treated as a **minimum**
  version, so a `cp310-abi3` wheel satisfies a request for 3.10 or any newer
  3.x. Version-agnostic `py3` wheels and source distributions are always kept.
- **Interpreter build** — by default only the standard GIL build is kept and
  free-threaded `cpXXXt` wheels are dropped; pass `--free-threaded` to invert
  this. ABI-agnostic (`abi3`/`py3`) wheels are kept in both modes.

## Notes & limitations

- You can pin the version of packages you request directly (on the command line
  or in a requirements file) using pip-style specifiers — an exact `==` pin, or
  a range like `>=13,<14` (ranges require `packaging` and select the highest
  matching release).
- For **transitive** dependencies, an exact `==` pin declared in a parent
  package's metadata is honoured; any other specifier (`>=`, `~=`, ranges, or no
  constraint) resolves to the latest release. The tool does **not** perform full
  version-constrained back-tracking resolution across the whole tree, and when a
  dependency is required by multiple parents the first specifier encountered (in
  breadth-first order) wins. If you need an exact, mutually-consistent pinned set
  across the entire tree, `pip download` is the right tool.
- Transitive dependencies contribute their unconditional runtime requirements,
  plus any extras explicitly requested of them by a parent package (e.g. a
  requirement of `pyjwt[crypto]` expands PyJWT's `crypto` extra and so pulls in
  `cryptography`). Extras you pass via `--extras` apply to the root package(s).
- Environment markers are evaluated against the **current** interpreter/OS.
  `--platform` and `--python-version` only affect *which downloaded files* are
  selected, not how markers are evaluated during resolution.

## Version History

### 1.2.2
- Fixed a bug where extras requested on transitive dependencies were ignored.
  A requirement like `pyjwt[crypto]` (as declared by `mcp`) now correctly
  expands PyJWT's `crypto` extra and pulls in `cryptography` and its
  dependencies. Extras are now propagated per-package through the graph.

### 1.2.1
- Added single-letter short aliases for the remaining options: `-e` (`--extras`),
  `-p` (`--platform`), `-P` (`--python-version`), `-w` (`--wheels-only`),
  `-n` (`--no-download`), and `-f` (`--free-threaded`).

### 1.2.0
- Transitive dependencies now honour exact `==` version pins declared in a
  parent package's metadata (e.g. `pydantic` pinning `pydantic-core==2.27.0`).
  Non-exact specifiers still resolve to the latest release.

### 1.1.0
- Added version pinning for root packages via pip-style specifiers
  (`pkg==1.2.3`, `pkg>=13,<14`), usable on the command line and in requirements
  files. Exact pins are fetched directly; ranges select the highest matching
  published release. The resolution summary now shows each package's version.

### 1.0.0
- First documented release: added `README.md`, the full `LICENSE` (GNU GPL
  v3.0), and author/license metadata in the script.

### 0.6.1
- Fixed Python-version filtering for stable-ABI wheels: a `cpXY-abi3` wheel
  (e.g. `pyside6-...-cp310-abi3-win_amd64.whl`) is now treated as a *minimum*
  version, so it correctly matches any newer 3.x request instead of being
  skipped as "no downloadable files".

### 0.6.0
- Added `-r`/`--requirements` to read package names from one or more
  requirements.txt-style files (comments, `pkg[extra]` syntax, and nested `-r`
  includes supported). Positional packages are now optional.

### 0.5.0
- Improved download-location handling: added `-o`/`--output` aliases, `~`
  expansion, automatic creation of (nested) target directories, and clear
  error reporting.

### 0.4.0
- Added `--free-threaded` to select no-GIL (`cpXXXt`) wheels; defaults to the
  standard GIL build.

### 0.3.0
- Added `--python-version` filtering (`3.13`, `3.13.7`, `313`, `cp313`), with
  the patch level ignored.

### 0.2.0
- Added `--platform` to download wheels for a target OS/architecture other than
  the current machine (family names or literal wheel platform tags).

### 0.1.0
- Initial version: recursive PyPI dependency resolution via the JSON API, with
  PEP 508 requirement/marker parsing (and a fallback parser), extras support,
  dependency-tree printing, and distribution-file downloading.

## Author

Ray Carioto &lt;raymond.carioto@gmail.com&gt;

## License

This project is licensed under the **GNU General Public License v3.0 (or later)**.

Copyright (C) 2026 Ray Carioto

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. This program is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See the [`LICENSE`](LICENSE) file or
<https://www.gnu.org/licenses/> for the full license text.
