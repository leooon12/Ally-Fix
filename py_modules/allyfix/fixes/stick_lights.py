"""Stick Light Fix: keep the joystick RGB rings off.

The rings are a multicolor LED class device of hid_asus_ally. After a reboot
the controller comes up with its default (blue, full brightness), and nothing
on SteamOS restores a previous setting. Writing brightness 0 through the LED
class keeps the rings dark across suspend (unlike turning them "off" together
with mcu_powersave, which lets the MCU reset them in sleep), so the fix writes
a static blue at brightness 0 once when the plugin starts, and again when the
LED device (re)appears. After resume it only checks, and writes only if the
rings are lit: a redundant write makes them flash.
"""

from __future__ import annotations

import asyncio
import glob
import os
from typing import Any

import decky

from ..base import Fix, cancel_task
from ..sysfs import dmi_vendor, read_int, read_str, write_str

LED_PATH = "/sys/class/leds/ally:rgb:joystick_rings"
LED_GLOB = "/sys/class/leds/*joystick_rings*"
ZONES = 4
COLOR = 0x0000FF  # blue, packed 0xRRGGBB per zone
RESUME_DELAY_S = 3.0
ADD_DELAY_S = 1.0


def _led_dir() -> str | None:
    if os.path.isdir(LED_PATH):
        return LED_PATH
    found = sorted(glob.glob(LED_GLOB))
    return found[0] if found else None


class StickLightsFix(Fix):
    id = "stick_lights"
    title = "Stick Light Fix"

    def __init__(self) -> None:
        super().__init__()
        self._uevent = None  # set by plugin
        self._task: asyncio.Task | None = None

    # --- Fix interface ---------------------------------------------------
    def supported(self) -> tuple[bool, str]:
        if "asus" not in dmi_vendor().lower():
            return False, "only for ASUS ROG Ally devices"
        if _led_dir() is None:
            return False, "stick light control not available"
        return True, ""

    def is_applied(self) -> bool:
        d = _led_dir()
        return d is not None and read_int(os.path.join(d, "brightness")) == 0

    async def apply(self) -> None:
        d = _led_dir()
        if d is None:
            raise OSError("stick light device not found")
        write_str(os.path.join(d, "multi_intensity"), " ".join([str(COLOR)] * ZONES))
        write_str(os.path.join(d, "brightness"), "0")
        decky.logger.info("[stick_lights] applied: blue, brightness 0")

    async def revert(self) -> None:
        d = _led_dir()
        if d is None:
            return
        mx = read_int(os.path.join(d, "max_brightness"), 255)
        write_str(os.path.join(d, "brightness"), str(mx))
        decky.logger.info("[stick_lights] reverted: brightness %d", mx)

    def details(self) -> dict[str, Any]:
        d = _led_dir()
        if d is None:
            return {}
        return {
            "brightness": read_int(os.path.join(d, "brightness")),
            "multi_intensity": read_str(os.path.join(d, "multi_intensity")),
        }

    async def on_resume(self) -> None:
        self._schedule("resume", RESUME_DELAY_S)

    async def start_background(self) -> None:
        if self._uevent is not None:
            self._uevent.subscribe("leds", self._on_led_event)

    async def stop_background(self) -> None:
        if self._uevent is not None:
            self._uevent.unsubscribe("leds", self._on_led_event)
        await cancel_task(self._task)
        self._task = None

    # --- re-apply ----------------------------------------------------------
    async def _on_led_event(self, event: dict[str, str]) -> None:
        if not self.enabled or event.get("ACTION") != "add":
            return
        if "joystick_rings" not in event.get("DEVPATH", ""):
            return
        self._schedule("led add", ADD_DELAY_S)

    def _schedule(self, reason: str, delay: float) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._reapply_later(reason, delay))

    async def _reapply_later(self, reason: str, delay: float) -> None:
        await asyncio.sleep(delay)
        # Brightness 0 normally survives suspend, and a redundant write is not free:
        # the driver lights the rings up for a moment before the new colour lands.
        if self.is_applied():
            decky.logger.info("[stick_lights] still off after %s, nothing to do", reason)
            return
        decky.logger.info("[stick_lights] re-applying after %s", reason)
        await self.reapply_if_enabled()
        await self.notify()
