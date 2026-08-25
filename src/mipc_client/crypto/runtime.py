"""Run the JavaScript helpers extracted from the MIPC website with QuickJS.

``js/md5.js``, ``js/mcodec.js`` and ``js/mdh.js`` are verbatim copies of the
scripts served by mipcm.com. They are evaluated as they are instead of being
translated to Python: the site's obfuscated one-letter globals collide between
the three files, so each script is wrapped in its own function scope and only
the object it is meant to export is published to the shared global scope.

The engine is the QuickJS-ng build embedded in DukPy. DukPy is named after
Duktape, which it used to embed, but it ships QuickJS since 0.6.0 and, unlike
the ``quickjs`` package, publishes wheels for every interpreter and platform
this runs on, so installing it never needs a compiler. Note that those wheels
are manylinux only: on a musl base image such as Alpine, pip builds it from
source and the image then needs a compiler.

QuickJS contexts are not thread safe, and creating one makes DukPy read its own
runtime scripts from disk, which must not happen on an event loop. Both problems
go away by giving the interpreter a thread of its own: every call is handed to
it, so the scripts are only ever loaded and run there.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

from dukpy import JSInterpreter

__all__ = ["call"]

_JS_DIR = Path(__file__).parent / "js"

# `md5.js` only declares loose functions, the two others build their own object.
_MODULES = {
    "md5": (
        "{hex: hex, b64: b64, str: str,"
        " hex_hmac: hex_hmac, b64_hmac: b64_hmac, str_hmac: str_hmac}"
    ),
    "mcodec": "mcodec",
    "mdh": "mdh",
}

# Entry points, called by name from `call()`. `mcodec.nid` hashes with whatever
# implementation it is handed, which is always `md5.hex` on the MIPC pages.
_ENTRIES = """
var entries = {
    parameters: function () {
        return {prime: mdh.prime, generator: mdh.g};
    },
    gen_private: function () {
        return mdh.gen_private();
    },
    gen_public: function (private_key) {
        return mdh.gen_public(private_key);
    },
    gen_shared_secret: function (private_key, public_key) {
        return mdh.gen_shared_secret(private_key, public_key);
    },
    nid: function (seq, id, shared_key, num) {
        return mcodec.nid(seq, id, shared_key, num, null, null, md5, "hex");
    },
};

function entry(name, args) {
    return entries[name].apply(null, args);
}
"""

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mipc_client_js")


def _scoped(name: str, exports: str) -> str:
    """Evaluate the `name` script in its own scope and export `exports` as `name`."""
    source = (_JS_DIR / f"{name}.js").read_text(encoding="utf-8")

    return (
        f"var {name} = (function () {{\n"
        f"var {name};\n{source}\n;return {exports};\n"
        "})();"
    )


@cache
def _interpreter() -> JSInterpreter:
    """Load the MIPC scripts into a QuickJS interpreter, on first use only."""
    interpreter = JSInterpreter()

    for name, exports in _MODULES.items():
        interpreter.evaljs(_scoped(name, exports))

    interpreter.evaljs(_ENTRIES)

    return interpreter


def _evaluate(name: str, args: list[Any]) -> Any:
    """Run one entry point. Always called on the interpreter's own thread."""
    return _interpreter().evaljs(
        "entry(dukpy['name'], dukpy['args']);", name=name, args=args
    )


def call(name: str, *args: Any) -> Any:
    """Call one of the JavaScript entry points and return its result."""
    return _EXECUTOR.submit(_evaluate, name, list(args)).result()
