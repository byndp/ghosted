"""Push adapters for Ghosted (Python 3.9+, stdlib only).

``get_adapter(cfg, env)`` picks the adapter named by ``[notify] adapter`` in config.toml:

- ``none``: no adapter (returns None); the digest stays in the terminal (``ghosted sweep``).
- ``ntfy`` (``notify.ntfy.NtfyAdapter``): the default for strangers; free, no signup.
- ``pushover`` (``notify.pushover.PushoverAdapter``): optional.
- ``macos`` (``notify.macos.MacAdapter``): a banner on this Mac through osascript (no phone).
- ``claude-app`` (``notify.claudeapp.ClaudeAppAdapter``): the Claude app on the phone, through a
  background Claude Code session (contract 16.12); falls back to ntfy when its topic is set.
- ``remote`` (``notify.remote.RemoteAdapter``): your own Worker + PWA. It ``owns_schedule``: the
  Worker decides the pushes, and ``ghosted run`` calls ``adapter.sync(ledger, now)`` instead of
  running ``core.schedule.decide`` and ``send``.

``env`` is the secrets mapping (``core.config.load_env(cfg)``, read when None is passed). Extra
keyword options go to the adapter's constructor (tests use them for fakes and short delays).
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Type

from notify.base import Adapter, SendResult, SyncResult, TransportError
from notify.claudeapp import ClaudeAppAdapter
from notify.macos import MacAdapter
from notify.ntfy import NtfyAdapter
from notify.pushover import PushoverAdapter
from notify.remote import RemoteAdapter

__all__ = ["get_adapter", "ADAPTERS", "Adapter", "SendResult", "SyncResult", "TransportError",
           "NtfyAdapter", "PushoverAdapter", "RemoteAdapter", "MacAdapter", "ClaudeAppAdapter"]

ADAPTERS = ("none", "ntfy", "pushover", "remote", "macos", "claude-app")
_CLASSES: Dict[str, Type[Adapter]] = {
    "claude-app": ClaudeAppAdapter,
    "ntfy": NtfyAdapter,
    "pushover": PushoverAdapter,
    "remote": RemoteAdapter,
    "macos": MacAdapter,
}


def get_adapter(cfg: Any, env: Optional[Mapping[str, str]] = None, **options: Any) -> Optional[Adapter]:
    """The configured adapter, or None for ``adapter = "none"``. Raises ValueError for an
    unknown name. Never raises for incomplete settings: ``check()`` and ``send()`` report those."""
    raw = getattr(cfg, "adapter", "none")
    name = "none" if raw is None else raw.strip().lower() if isinstance(raw, str) else raw
    if name == "none":
        return None
    cls = _CLASSES.get(name) if isinstance(name, str) else None
    if cls is None:
        raise ValueError("unknown notify adapter %r: use one of %s" % (raw, ", ".join(ADAPTERS)))
    if env is None:
        from core.config import load_env
        env = load_env(cfg)
    return cls(cfg, env, **options)  # type: ignore[call-arg]
