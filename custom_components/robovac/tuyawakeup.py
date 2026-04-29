# -*- coding: utf-8 -*-

"""Tuya UDP wakeup broadcast for protocol 3.4/3.5 devices.

Some Tuya devices (e.g. the Eufy T2276 vacuum) enter a deep sleep state
where the TCP port 6668 listener is disabled. The official Tuya mobile app
wakes them by sending a UDP broadcast on port 7000 with a protocol 3.5-framed
REQ_DEVINFO message encrypted with the well-known Tuya UDP key.

This module replicates that broadcast so Home Assistant can wake the device
before attempting a TCP connection.

The broadcast payload and framing match the behaviour observed in tcpdump
captures of the Tuya app and the TinyTuya scanner implementation.
"""

import asyncio
import json
import logging
import os
import socket
import struct
import time
from hashlib import md5
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Well-known Tuya UDP encryption key (same key used by tuyalocaldiscovery.py)
UDP_KEY = md5(b"yGAdlopoPVldABfn").digest()
_AESGCM_UDP = AESGCM(UDP_KEY)

# Protocol 3.5 framing constants
_PREFIX_35 = 0x00006699
_SUFFIX_35 = 0x00009966
# Header format: prefix(4) + version(1) + reserved(1) + seq(4) + cmd(4) + len(4) = 18 bytes
_HEADER_FORMAT = ">IBBIII"
# Suffix format: tag(16) + suffix(4)
_SUFFIX_FORMAT = ">16sI"

# Command 0x25 (37): Active Device Discovery / Wake Up (REQ_DEVINFO)
_CMD_REQ_DEVINFO = 0x25

# Destination port for wakeup broadcasts
_WAKEUP_PORT = 7000

# Module-level cooldown state
_last_broadcast_time: float = 0.0


def _build_wakeup_packet(local_ip: str) -> bytes:
    """Build a protocol 3.5 wakeup broadcast packet.

    Constructs the exact same packet format the Tuya mobile app sends:
    - Protocol 3.5 header with magic prefix 0x00006699
    - Command 0x25 (REQ_DEVINFO)
    - AES-GCM encrypted JSON payload: {"from":"app","ip":"<local_ip>"}
    - GCM authentication tag
    - Magic suffix 0x00009966

    Args:
        local_ip: The local IP address to include in the payload.

    Returns:
        The complete packet bytes ready to send via UDP.
    """
    # Build the plaintext payload
    payload_json = json.dumps({"from": "app", "ip": local_ip}).encode("utf-8")

    # Calculate sizes for the header
    # payload_size in the header covers: IV(12) + ciphertext + tag(16)
    payload_size = 12 + len(payload_json) + 16

    # Build the header
    header = struct.pack(
        _HEADER_FORMAT,
        _PREFIX_35,   # prefix
        0x00,         # version
        0x00,         # reserved
        0x00000000,   # sequence
        _CMD_REQ_DEVINFO,  # command
        payload_size,      # payload length
    )

    # AAD = header bytes after the 4-byte prefix (14 bytes)
    aad = header[4:]

    # Encrypt with AES-GCM
    iv = os.urandom(12)
    ct_with_tag = _AESGCM_UDP.encrypt(iv, payload_json, aad)
    ciphertext = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]

    # Build the footer: tag(16) + suffix(4)
    footer = struct.pack(_SUFFIX_FORMAT, tag, _SUFFIX_35)

    return header + iv + ciphertext + footer


async def _async_get_source_ip(
    hass: "HomeAssistant | None",
    target_ip: str | None = None,
) -> str:
    """Get the local IP address to use in the wakeup payload.

    Uses Home Assistant's network component to determine the correct
    source IP for reaching the target device. Falls back to "0.0.0.0"
    if hass is unavailable (e.g. during early startup before the entity
    is added to HA).

    Args:
        hass: The Home Assistant instance, or None.
        target_ip: Optional target IP to route towards.

    Returns:
        The local IP address as a string.
    """
    if hass is not None:
        try:
            from homeassistant.components.network import async_get_source_ip
            return await async_get_source_ip(hass, target_ip=target_ip)
        except Exception:
            _LOGGER.debug(
                "Could not determine source IP via HA network component, "
                "falling back to 0.0.0.0",
                exc_info=True,
            )
    return "0.0.0.0"


async def async_send_wakeup_broadcast(
    hass: "HomeAssistant | None" = None,
    target_ip: str | None = None,
    cooldown_seconds: float = 5.0,
) -> bool:
    """Send a Tuya protocol 3.5 wakeup broadcast on UDP port 7000.

    This wakes devices from deep sleep so their TCP listener becomes
    available for direct local control.

    The broadcast is rate-limited by a module-level cooldown to avoid
    flooding the network when multiple devices or rapid reconnection
    attempts trigger it.

    Args:
        hass: The Home Assistant instance (used to determine the local IP).
        target_ip: Optional target device IP for source IP routing.
        cooldown_seconds: Minimum time between broadcasts. Defaults to 5.0.

    Returns:
        True if the broadcast was sent, False if skipped due to cooldown.
    """
    global _last_broadcast_time

    now = time.monotonic()
    if now - _last_broadcast_time < cooldown_seconds:
        _LOGGER.debug(
            "Wakeup broadcast skipped: cooldown active (%.1fs remaining)",
            cooldown_seconds - (now - _last_broadcast_time),
        )
        return False

    local_ip = await _async_get_source_ip(hass, target_ip)
    packet = _build_wakeup_packet(local_ip)

    try:
        # Create a UDP broadcast socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)

        # Send the broadcast — run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: sock.sendto(packet, ("255.255.255.255", _WAKEUP_PORT)),
        )
        sock.close()

        _last_broadcast_time = time.monotonic()
        _LOGGER.debug(
            "Sent wakeup broadcast from %s to 255.255.255.255:%d (%d bytes) hex=%s",
            local_ip,
            _WAKEUP_PORT,
            len(packet),
            packet.hex(),
        )
        return True

    except Exception:
        _LOGGER.warning(
            "Failed to send wakeup broadcast on UDP port %d", _WAKEUP_PORT,
            exc_info=True,
        )
        return False
