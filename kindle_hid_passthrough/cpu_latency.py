#!/usr/bin/env python3
"""Block cpuidle states too slow for the HCI UART to survive (issue #120).

On i.MX Kindles the deepest cpuidle state (LOW-POWER-IDLE: ARM platform off,
24 MHz OSC, DDR in self-refresh) has an exit latency far longer than the UART
RX FIFO can buffer at 2 Mbaud. If the Broadcom chip transmits while the SoC is
that deep, the leading bytes are gone before the UART is clocked again. We also
disable bluesleep to keep the chip awake, which tears down the HOST_WAKE IRQ,
so nothing wakes the SoC ahead of the data.

Preferred mechanism is a PM QoS CPU_DMA_LATENCY request: the constraint lives
as long as the fd is open, so it releases on crash and survives suspend with no
bookkeeping. Falls back to the per-state sysfs disable flag where the misc
device is missing, saving and restoring the original values.
"""

import glob
import os
import struct
import time

from logging_utils import log

__all__ = ['CpuLatencyHold', 'boost_cpu']

CPU_DMA_LATENCY_DEV = '/dev/cpu_dma_latency'
CPUIDLE_GLOB = '/sys/devices/system/cpu/cpu*/cpuidle'

UART_RX_FIFO_BYTES = 32   # i.MX UART RX FIFO depth
BITS_PER_BYTE = 10        # 8N1 on the wire
FIFO_SAFETY = 0.5         # the RX interrupt trips well before the FIFO is full

CPU_BOOST_PATH = '/sys/devices/system/cpu/cpufreq/ondemand/override'
CPU_BOOST_SECONDS = '2'
CPU_BOOST_INTERVAL = 1.0

_last_boost = 0.0


def boost_cpu():
    """Give input the full-speed window the Kindle gives a touch (#225)."""
    global _last_boost
    now = time.monotonic()
    if now - _last_boost < CPU_BOOST_INTERVAL:
        return
    _last_boost = now
    try:
        with open(CPU_BOOST_PATH, 'w') as f:
            f.write(CPU_BOOST_SECONDS)
    except OSError:
        pass


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _states(cpuidle_dir):
    """[(index, name, exit_latency_us)] for one CPU, ordered by index."""
    found = []
    for path in glob.glob(f'{cpuidle_dir}/state*'):
        try:
            index = int(os.path.basename(path)[5:])
        except ValueError:
            continue
        latency = _read_int(f'{path}/latency')
        if latency is None:
            continue
        try:
            with open(f'{path}/name') as f:
                name = f.read().strip()
        except OSError:
            name = f'state{index}'
        found.append((index, name, latency))
    return sorted(found)


class CpuLatencyHold:
    """Hold a cpuidle exit-latency ceiling while the HCI UART is open."""

    def __init__(self, baud_rate):
        self.baud_rate = baud_rate or 2000000
        self._fd = None
        self._restore = {}  # sysfs disable path -> original value

    @property
    def budget_us(self):
        """How long the RX FIFO can cover a stalled CPU at this baud rate."""
        seconds = UART_RX_FIFO_BYTES * BITS_PER_BYTE / self.baud_rate
        return seconds * 1e6 * FIFO_SAFETY

    def _plan(self):
        """(target_latency_us, [blocked state paths]) or None if nothing to do."""
        dirs = sorted(glob.glob(CPUIDLE_GLOB))
        if not dirs:
            return None

        states = _states(dirs[0])
        if len(states) < 2:
            return None

        budget = self.budget_us
        allowed = [s for s in states if s[2] <= budget]
        # Never drop below the shallowest non-WFI state: the evidence is that it
        # is fine, and forcing WFI-only would cost real battery.
        if len(allowed) < 2:
            allowed = states[:2]
        if len(allowed) == len(states):
            return None

        target = max(s[2] for s in allowed)
        blocked = [s for s in states if s not in allowed]
        log.info(
            f"cpuidle budget {budget:.0f}us at {self.baud_rate} baud: "
            f"allowing {', '.join(s[1] for s in allowed)}; "
            f"blocking {', '.join(f'{s[1]}({s[2]}us)' for s in blocked)}")

        paths = [f'{d}/state{s[0]}/disable' for d in dirs for s in blocked]
        return target, paths

    def acquire(self):
        if self._fd is not None or self._restore:
            return
        plan = self._plan()
        if plan is None:
            return
        target, paths = plan

        try:
            fd = os.open(CPU_DMA_LATENCY_DEV, os.O_WRONLY)
        except OSError as e:
            log.info(f"No {CPU_DMA_LATENCY_DEV} ({e}); using cpuidle sysfs")
            self._acquire_sysfs(paths)
            return

        try:
            os.write(fd, struct.pack('i', int(target)))
        except OSError as e:
            os.close(fd)
            log.warning(f"Could not set PM QoS latency: {e}; using cpuidle sysfs")
            self._acquire_sysfs(paths)
            return

        self._fd = fd
        log.info(f"Holding CPU latency <= {int(target)}us for the HCI UART")

    def _acquire_sysfs(self, paths):
        for path in paths:
            original = _read_int(path)
            if original is None:
                continue
            try:
                with open(path, 'w') as f:
                    f.write('1')
            except OSError as e:
                log.warning(f"Could not disable {path}: {e}")
                continue
            self._restore[path] = original
        if self._restore:
            log.info(f"Disabled {len(self._restore)} deep cpuidle state(s) "
                     f"for the HCI UART")

    def release(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            log.info("Released CPU latency hold")

        for path, original in self._restore.items():
            try:
                with open(path, 'w') as f:
                    f.write(str(original))
            except OSError as e:
                log.warning(f"Could not restore {path}: {e}")
        if self._restore:
            log.info("Restored cpuidle states")
            self._restore = {}
