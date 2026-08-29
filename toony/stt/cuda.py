"""Finding the CUDA libraries CTranslate2 needs, without an environment variable.

``pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`` puts the shared objects
inside site-packages, where the dynamic linker does not look. The usual advice
is to set ``LD_LIBRARY_PATH``, which is fragile: it has to be set before the
process starts, so it has to live in the systemd unit, and it is lost the moment
anything rewrites that unit.

Loading them by absolute path with ``RTLD_GLOBAL`` before CTranslate2 asks for
them does the same job from inside the process. Once a library is in the
process with its symbols global, the later ``dlopen`` by soname finds it.
"""

from __future__ import annotations

import ctypes
import functools
import os
import sys
from pathlib import Path

from ..log import get

log = get("stt.cuda")

# In dependency order: cuBLAS needs cuBLASLt, and cuDNN's own pieces need the
# base library, so loading them out of order fails on the first one.
WANTED = [
    "libcudart.so.12",
    "libcublasLt.so.12",
    "libcublas.so.12",
    "libcudnn.so.9",
    "libcudnn_ops.so.9",
    "libcudnn_cnn.so.9",
    "libcudnn_engines_precompiled.so.9",
    "libcudnn_engines_runtime_compiled.so.9",
    "libcudnn_heuristic.so.9",
    "libcudnn_graph.so.9",
    "libcudnn_adv.so.9",
]

# The ones without which transcription cannot run at all.
REQUIRED = ("libcublas.so.12", "libcudnn_ops.so.9")


def _site_package_dirs() -> list[Path]:
    """Every nvidia/*/lib directory pip may have installed, newest path first."""
    roots: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        nvidia = Path(entry) / "nvidia"
        if nvidia.is_dir():
            roots.append(nvidia)
    out: list[Path] = []
    for root in roots:
        for child in sorted(root.iterdir()):
            lib = child / "lib"
            if lib.is_dir():
                out.append(lib)
    return out


def library_path() -> str:
    """What LD_LIBRARY_PATH would need to be. Used by `toony install`."""
    return os.pathsep.join(str(p) for p in _site_package_dirs())


@functools.lru_cache(maxsize=1)
def preload() -> dict[str, str]:
    """Load the CUDA libraries into this process. Returns what was found where.

    Safe to call when there is no GPU and no CUDA: everything that fails is
    skipped, and the caller falls back to the CPU.
    """
    found: dict[str, str] = {}
    directories = _site_package_dirs()

    for name in WANTED:
        # Already loadable — a system CUDA install, or LD_LIBRARY_PATH is set.
        try:
            ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
            found[name] = name
            continue
        except OSError:
            pass
        for directory in directories:
            candidate = directory / name
            if not candidate.exists():
                continue
            try:
                ctypes.CDLL(str(candidate), mode=ctypes.RTLD_GLOBAL)
                found[name] = str(candidate)
                break
            except OSError as exc:
                log.debug("could not load %s: %s", candidate, exc)

    if found:
        log.debug("preloaded %d CUDA libraries", len(found))
    return found


def missing() -> list[str]:
    """The required libraries that still cannot be loaded after preloading."""
    loaded = preload()
    return [name for name in REQUIRED if name not in loaded]


def usable() -> bool:
    return not missing()


def advice() -> str:
    absent = missing()
    if not absent:
        return ""
    if not _site_package_dirs():
        return ("The CUDA maths libraries are not installed. Either:\n"
                "  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
                "or run speech recognition on the CPU:\n"
                "  toony config set stt.local.device cpu")
    return (f"{', '.join(absent)} is installed but will not load. "
            "It is usually a version mismatch between ctranslate2 and the "
            "nvidia packages. Either reinstall them:\n"
            "  pip install -U ctranslate2 nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
            "or run on the CPU:\n"
            "  toony config set stt.local.device cpu")
