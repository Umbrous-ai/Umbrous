"""Ollama exposure check: is Ollama reachable from other machines?

Ollama has no login. If it listens on more than loopback (for example because
OLLAMA_HOST=0.0.0.0 was set so a phone could use it), anyone on the same
network can use your models and see what they are asked. Umbrous cannot change
Ollama's settings, but it can look and warn.

What the check does:
  1. Is Ollama answering on 127.0.0.1? (If not, there is nothing to expose.)
  2. Try to connect to Ollama's port on each of this Mac's own network
     addresses (Wi-Fi, Ethernet). If a connection is accepted there, Ollama is
     listening beyond loopback.
  3. Report the OLLAMA_HOST setting it can see (this process's environment and,
     on macOS, `launchctl getenv`, which is where the Ollama app reads it).

What it cannot see: the macOS firewall, your router, VPNs, or IPv6-only
exposure. A "loopback only" result means "not listening on these addresses",
not "safe on every network".
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional

LIMITS = "Cannot see the macOS firewall, router, VPN or IPv6 settings."


def local_ipv4_addresses() -> List[str]:
    """This machine's own non-loopback IPv4 addresses."""
    found = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # picks a route; sends nothing
        found.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2"):
            try:
                out = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=2)
                if out.returncode == 0 and out.stdout.strip():
                    found.add(out.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                pass
    return sorted(a for a in found if a and not a.startswith("127.") and a != "0.0.0.0")


def ollama_host_setting() -> Optional[str]:
    value = os.environ.get("OLLAMA_HOST")
    if value:
        return value
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["launchctl", "getenv", "OLLAMA_HOST"], capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _accepts(addr: str, port: int, timeout: float) -> bool:
    try:
        socket.create_connection((addr, port), timeout=timeout).close()
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class ExposureReport:
    status: str                     # "loopback_only" | "exposed" | "not_running"
    port: int
    reachable_on: List[str] = field(default_factory=list)   # non-loopback addresses that accepted a connection
    checked: List[str] = field(default_factory=list)
    ollama_host: Optional[str] = None
    limits: str = LIMITS

    @property
    def summary(self) -> str:
        if self.status == "exposed":
            return f"Exposed: Ollama accepts connections on {', '.join(self.reachable_on)}:{self.port}"
        if self.status == "not_running":
            return "Ollama is not running on 127.0.0.1"
        return f"Loopback only (checked {len(self.checked)} network address(es))"

    def as_dict(self) -> dict:
        return {"status": self.status, "summary": self.summary, "port": self.port,
                "reachable_on": self.reachable_on, "checked": self.checked,
                "ollama_host": self.ollama_host, "limits": self.limits}


def check_exposure(port: int = 11434, addresses: Optional[List[str]] = None, timeout: float = 0.5,
                   accepts: Callable[[str, int, float], bool] = _accepts) -> ExposureReport:
    setting = ollama_host_setting()
    if not accepts("127.0.0.1", port, timeout):
        return ExposureReport("not_running", port, ollama_host=setting)
    checked = local_ipv4_addresses() if addresses is None else addresses
    reachable = [a for a in checked if accepts(a, port, timeout)]
    return ExposureReport("exposed" if reachable else "loopback_only", port, reachable, checked, setting)
