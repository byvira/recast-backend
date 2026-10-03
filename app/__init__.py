# module init
#
# Complex-script text shaping (Tamil, Hindi): Pillow's Linux builds carry the shaping engine but load the FriBiDi library from
# the host when Pillow is first imported, and a plain server image may not have it. A copy of that library ships with the project
# (app/pipelines/media/native, LGPL, licence file beside it); loading it here, before anything imports Pillow, lets Pillow find it
# by name. Only on Linux, only if the file is there, and a failure changes nothing (pictures are then drawn without shaping and
# say so on the picture). Standard library only, so it is safe to run this early.
import sys as _sys

if _sys.platform.startswith("linux"):
    try:
        import ctypes as _ctypes
        from pathlib import Path as _Path

        _fribidi = _Path(__file__).resolve().parent / "pipelines" / "media" / "native" / "libfribidi.so.0"
        if _fribidi.exists():
            _ctypes.CDLL(str(_fribidi), mode=_ctypes.RTLD_GLOBAL)
    except Exception:  # noqa: BLE001
        pass
