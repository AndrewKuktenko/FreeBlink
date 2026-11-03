"""Blink Mini Pan-Tilt Mount control over the live-view (immis) session.

The mount has no REST endpoint. Commands are extra immis frames written to the same
TLS socket that carries the live video, and the mount reports its angle back on it.

Protocol constants and layouts are from blink-camera-mcp by Abhijat Saxena (MIT
License, https://github.com/AbhijatSaxena/blink-camera-mcp), which recovered them
from the official Android app and verified them on a Blink Mini with the mount.
"""

import struct
from dataclasses import dataclass
from typing import Optional

HEADER = ">BII"  # msgtype, identifier, payload length

INLINE_COMMAND = 0x14     # client -> camera; identifier = command id
ACCESSORY_MESSAGE = 0x15  # camera -> client; identifier = message id

CMD_MOVE = 3
CMD_STOP = 4
CMD_GO_HOME = 5
CMD_SET_HOME = 6
CMD_PAN_OVERVIEW = 7

MSG_POSITION = 2
MSG_HOME_POSITION = 3

STATUS_MOVING = 0x10
ANGLE_MIN, ANGLE_MAX = -128, 127


@dataclass
class Position:
    pan: int
    tilt: int
    moving: bool


def _frame(identifier: int, payload: bytes = b"") -> bytes:
    return struct.pack(HEADER, INLINE_COMMAND, identifier, len(payload)) + payload


def clamp(angle: int) -> int:
    return max(ANGLE_MIN, min(ANGLE_MAX, int(angle)))


def move(pan: int, tilt: int) -> bytes:
    """Move to an absolute angle in degrees (the mount stops at its own physical limits)."""
    return _frame(CMD_MOVE, bytes([0, 0, 0, 0, clamp(pan) & 0xFF, clamp(tilt) & 0xFF, 0]))


def stop() -> bytes:
    return _frame(CMD_STOP)


def go_home() -> bytes:
    return _frame(CMD_GO_HOME)


def set_home() -> bytes:
    return _frame(CMD_SET_HOME)


def pan_overview() -> bytes:
    return _frame(CMD_PAN_OVERVIEW)


def parse_position(identifier: int, payload: bytes) -> Optional[Position]:
    """Position from an ACCESSORY_MESSAGE, or None if the message isn't a position report."""
    if identifier not in (MSG_POSITION, MSG_HOME_POSITION) or len(payload) < 7:
        return None
    _counter, pan, tilt, status = struct.unpack(">IbbB", payload[:7])
    return Position(pan=pan, tilt=tilt, moving=status == STATUS_MOVING)
