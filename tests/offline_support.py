"""Optional publishing dependencies are not needed for offline Telegram tests."""

import sys
import types


def prepare_publish_stubs():
    try:
        import jwt  # noqa: F401
    except ModuleNotFoundError:
        module = types.ModuleType("jwt")
        module.encode = lambda *args, **kwargs: "offline-token"
        sys.modules["jwt"] = module
    try:
        import markdown  # noqa: F401
    except ModuleNotFoundError:
        module = types.ModuleType("markdown")
        module.markdown = lambda text, **kwargs: text
        sys.modules["markdown"] = module
