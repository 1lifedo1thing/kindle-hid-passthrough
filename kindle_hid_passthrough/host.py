#!/usr/bin/env python3
"""HID Host — runs BLE + Classic handlers on a single Bumble device."""

import asyncio
import time
from dataclasses import dataclass
from typing import List, Optional

from bumble.core import InvalidStateError
from bumble.hci import HCI_LE_SET_PRIVACY_MODE_COMMAND, HCI_LE_Set_Privacy_Mode_Command, HCI_Write_Class_Of_Device_Command, HCI_Write_Local_Name_Command

from ble import BLEMixin
from bt_setup import ensure_uhid
from classic import ClassicMixin
from config import Protocol, clean_device_name, config, get_version, normalize_addr
from device_cache import DeviceCache
from logging_utils import log
from pairing import create_keystore, create_pairing_config
from transport import create_bumble_device
from uhid_handler import Bus, UHIDDevice, descriptor_is_pointer, strip_digitizer_collections

__all__ = ['HIDHost']


@dataclass
class DeviceConfig:
    """Device configuration from devices.conf."""
    address: str
    protocol: Protocol
    name: Optional[str] = None


class DeviceSession:
    """Live state for one connected device, keyed by normalized address."""

    def __init__(self, address: str, protocol: Protocol, connection):
        self.address = address
        self.raw_address = str(connection.peer_address)
        self.protocol = protocol
        self.connection = connection
        self.peer = None
        self.channels = None
        self.name = None
        self.report_map: Optional[bytes] = None
        self.hid_reports = []
        self.uhid_device = None
        self.is_pointer = False
        self.last_report = None
        self.setup_task = None
        self.closed = False
        self.teardown_done = asyncio.Event()
        self.auth_failure = False
        self.vc_unplug = False
        self.battery_level: Optional[int] = None
        self.battery_updated: Optional[float] = None

    def is_alive(self) -> bool:
        conn = self.connection
        if conn is None:
            return False
        if not hasattr(conn, 'handle') or conn.handle is None:
            return False
        if getattr(conn, 'is_disconnected', False):
            return False
        return True

    def state_dict(self) -> dict:
        entry = {
            "address": self.address,
            "protocol": self.protocol.value,
            "name": self.name,
            "hid_ready": self.uhid_device is not None,
        }
        if self.uhid_device:
            entry["uhid_name"] = self.uhid_device.name
            if self.uhid_device.input_paths:
                entry["input_paths"] = self.uhid_device.input_paths
        if self.report_map:
            entry["descriptor_size"] = len(self.report_map)
        if self.battery_level is not None:
            entry["battery_level"] = self.battery_level
            entry["battery_updated"] = self.battery_updated
        return entry

    async def cleanup(self):
        """Idempotent release of session-owned resources."""
        if self.closed:
            await self.teardown_done.wait()
            return
        self.closed = True
        try:
            if self.uhid_device:
                try:
                    self.uhid_device.destroy()
                except Exception:
                    pass
                self.uhid_device = None
            if self.channels:
                if self.is_alive():
                    await self.channels.disconnect()
                self.channels = None
            if self.is_alive():
                try:
                    await asyncio.wait_for(self.connection.disconnect(), timeout=2.0)
                except asyncio.TimeoutError:
                    log.warning(f"Disconnect timed out for {self.address}")
                except Exception as e:
                    log.debug(f"Disconnect cleanup: {e}")
            self.connection = None
            self.peer = None
        finally:
            self.teardown_done.set()


class HIDHost(ClassicMixin, BLEMixin):
    """HID Host supporting both BLE and Classic Bluetooth.

    Protocol-specific handlers live in ClassicMixin and BLEMixin.
    This class owns init, start, run, sessions, pairing dispatch, and cleanup.
    """

    PROTOCOL_NAME = "HID"

    ACTIVE_DELAY = 2.0
    ACTIVE_RETRY_INTERVAL = 5.0
    ACTIVE_RETRY_INTERVAL_CONNECTED = 60.0
    ACTIVE_CONNECT_TIMEOUT = 10
    FIRST_SESSION_TIMEOUT = 60.0
    SETUP_TIMEOUT = 45.0

    def __init__(self, transport_spec: str = None):
        self.transport_spec = transport_spec or config.transport
        self.transport = None
        self.device = None

        self.sessions = {}
        self._pairing_session = None
        self._classic_psm_registered = False

        self._connection_tasks: set = set()

        self.classic_devices: List[DeviceConfig] = []
        self.ble_devices: List[DeviceConfig] = []
        self._keystore_addresses: set = set()
        self._keystore_address_types: dict = {}

        self.keystore = create_keystore(config.pairing_keys_file)
        self.device_cache = DeviceCache(config.cache_dir)

        # Set by the daemon: called with True when the first pointer device
        # gets its UHID device, False when the last goes away (drives the cursor).
        self.on_pointer_change = None

        self._sessions_changed = None
        self._radio_lock = None

    @property
    def connection_state(self) -> dict:
        """Current connection state as a dict for API consumers."""
        connections = [s.state_dict() for s in list(self.sessions.values())
                       if s.is_alive()]
        return {"connected": bool(connections), "connections": connections}

    def _parse_devices(self):
        """Parse devices from config and group by protocol."""
        devices = config.get_all_devices()
        self.classic_devices = []
        self.ble_devices = []

        for addr, protocol, name in devices:
            dev = DeviceConfig(address=addr, protocol=protocol, name=name)
            if protocol == Protocol.CLASSIC:
                self.classic_devices.append(dev)
            else:
                self.ble_devices.append(dev)

        log.info(f"Devices: {len(self.classic_devices)} Classic, {len(self.ble_devices)} BLE")

    async def start(self, pairing: bool = False):
        """Initialize the Bumble device with both protocols."""
        log.info(f"HID Host v{get_version()}")

        def configure(device):
            device.classic_enabled = bool(self.classic_devices)
            device.le_enabled = bool(self.ble_devices)
            if pairing:
                # No inbound links while pairing; run mode re-enables page scan.
                device.connectable = False
                device.discoverable = False
            device.keystore = self.keystore
            device.pairing_config_factory = create_pairing_config
            if self.classic_devices:
                device.classic_ssp_enabled = True
                device.classic_sc_enabled = True

        self.transport, self.device = await create_bumble_device(
            self.transport_spec, configure=configure)

        if self.device.address_resolution_offload:
            await self._set_device_privacy_modes()
            log.info("Controller address resolution enabled")

        # Classic-specific setup
        if self.classic_devices:
            class_of_device = 0x000104  # Computer/Desktop
            await self.device.host.send_command(
                HCI_Write_Class_Of_Device_Command(class_of_device=class_of_device),
                check_result=True
            )
            log.info(f"Classic enabled: CoD 0x{class_of_device:06X}")

            local_name_bytes = config.device_name.encode('utf-8') + b'\x00'
            await self.device.host.send_command(
                HCI_Write_Local_Name_Command(local_name=local_name_bytes),
                check_result=True
            )

        if self.ble_devices:
            log.info("BLE enabled")

        # Load keystore addresses
        await self._load_keystore_addresses()


    async def _set_device_privacy_modes(self):
        """Keep bonded peers visible when they advertise with their
        identity address instead of an RPA."""
        if not self.device.host.supports_command(HCI_LE_SET_PRIVACY_MODE_COMMAND):
            return
        for _, address in await self.keystore.get_resolving_keys():
            try:
                await self.device.send_command(
                    HCI_LE_Set_Privacy_Mode_Command(
                        peer_identity_address_type=address.address_type,
                        peer_identity_address=address,
                        privacy_mode=HCI_LE_Set_Privacy_Mode_Command.PrivacyMode.DEVICE_PRIVACY_MODE,
                    ), check_result=True)
            except Exception as e:
                log.warning(f"Privacy mode for {address}: {e}")

    async def _load_keystore_addresses(self):
        """Load addresses from keystore for connection filtering."""
        self._keystore_addresses = set()
        self._keystore_address_types = {}
        if self.keystore:
            try:
                keys = await self.keystore.get_all()
                if keys:
                    for entry in keys:
                        addr = str(entry[0]) if isinstance(entry, (list, tuple)) else str(entry)
                        self._keystore_addresses.add(normalize_addr(addr))
                        pairing_keys = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None
                        if pairing_keys is not None and pairing_keys.address_type is not None:
                            self._keystore_address_types[normalize_addr(addr)] = pairing_keys.address_type
                    log.info(f"Keystore has {len(self._keystore_addresses)} entries")
            except Exception as e:
                log.warning(f"Failed to load keystore: {e}")

    def _configured_name(self, addr: str) -> Optional[str]:
        """Return the configured devices.conf name for addr, if any."""
        if not addr:
            return None
        norm = normalize_addr(addr)
        for dev in self.classic_devices + self.ble_devices:
            if normalize_addr(dev.address) == norm and dev.name:
                return dev.name
        return None

    def _format_device(self, addr: str) -> str:
        """Format device address with name if available."""
        name = self._configured_name(addr)
        return f"{name} ({addr})" if name else addr

    async def run(self):
        """Main run loop - serve all configured devices concurrently."""
        self._sessions_changed = asyncio.Event()
        self._radio_lock = asyncio.Lock()

        self._parse_devices()
        await self.start()

        for dev in self.classic_devices + self.ble_devices:
            if dev.address != '*':
                cache = self.device_cache.load(dev.address)
                if cache and 'report_map' in cache:
                    log.info(f"Cached descriptor for {self._format_device(dev.address)}")

        await self._serve()

    async def _serve(self):
        """Run the protocol handlers and watchdog until failure or cancel."""
        tasks = []

        if self.classic_devices:
            tasks.append(asyncio.create_task(
                self._run_classic_handler(),
                name="classic_handler"
            ))

        if self.ble_devices:
            tasks.append(asyncio.create_task(
                self._run_ble_handler(),
                name="ble_handler"
            ))
            tasks.append(asyncio.create_task(
                self._run_ble_battery_poller(),
                name="ble_battery_poller"
            ))

        if not tasks:
            log.error("No devices configured")
            return

        log.info(f"Serving devices (Classic: {len(self.classic_devices)}, BLE: {len(self.ble_devices)})")

        tasks.append(asyncio.create_task(
            self._session_watchdog(), name="session_watchdog"))

        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise InvalidStateError(
                f"{next(iter(done)).get_name()} exited unexpectedly")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _session_watchdog(self):
        """Ask the daemon for a transport rebuild if nothing ever connects.

        Once a device has connected, an empty session set is a device that
        went to sleep, not a broken transport: the handlers keep initiating
        on the open radio, so rebuilding would only make us deaf for a while.
        """
        had_session = False
        while True:
            self._sessions_changed.clear()
            if self.sessions or had_session:
                had_session = True
                await self._sessions_changed.wait()
                continue
            try:
                await asyncio.wait_for(
                    self._sessions_changed.wait(),
                    timeout=self.FIRST_SESSION_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("Connection timeout - no device connected")
                raise InvalidStateError("No device connected within timeout")

    def _notify_sessions_changed(self):
        if self._sessions_changed:
            self._sessions_changed.set()

    def _track_task(self, task):
        self._connection_tasks.add(task)
        task.add_done_callback(self._connection_tasks.discard)
        return task

    # ==================== SESSIONS ====================

    def _new_session(self, address: str, protocol: Protocol, connection) -> DeviceSession:
        """Session factory for the protocol mixins (avoids circular imports)."""
        return DeviceSession(address, protocol, connection)

    def _register_session(self, session: DeviceSession):
        self.sessions[session.address] = session
        self._notify_sessions_changed()
        session.connection.on(
            'disconnection',
            lambda reason: self._on_session_disconnection(session, reason))

    async def _reject_connection(self, connection):
        try:
            await connection.disconnect()
        except Exception:
            pass

    def _on_session_disconnection(self, session: DeviceSession, reason):
        proto = session.protocol.value.upper()
        log.warning(f"[{proto}] Device disconnected: {session.address} (reason={reason})")

        if reason == 5 and session.protocol == Protocol.CLASSIC:
            log.info("[Classic] Authentication failure - will clear stale key and retry")
            session.auth_failure = True

        self._track_task(asyncio.create_task(self._teardown_session(session)))

    async def _run_session_setup(self, session: DeviceSession, setup):
        """Drive a session's HID setup, tearing it down on failure."""
        try:
            await asyncio.wait_for(setup, timeout=self.SETUP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning(f"Setup timed out for {self._format_device(session.address)}")
            await self._teardown_session(session)
        except Exception as e:
            log.warning(f"Setup failed for {self._format_device(session.address)}: {e}")
            await self._teardown_session(session)

    async def _teardown_session(self, session: DeviceSession):
        """End one session and apply its post-disconnect policy."""
        if self.sessions.get(session.address) is session:
            del self.sessions[session.address]
            self._notify_sessions_changed()

        setup = session.setup_task
        if setup and not setup.done() and setup is not asyncio.current_task():
            setup.cancel()
            try:
                await setup
            except (asyncio.CancelledError, Exception):
                pass

        if session.closed:
            await session.teardown_done.wait()
            return
        was_pointer = session.is_pointer and session.uhid_device is not None
        await session.cleanup()
        if was_pointer and self._pointer_count() == 0:
            self._notify_pointer(False)

        if session.auth_failure:
            log.info(f"Auth failure for {session.address}, clearing stale key")
            config.remove_pairing_key(session.address)
        if session.vc_unplug:
            log.info(f"Virtual cable unplugged by {session.address}, removing device")
            config.remove_device(session.address)

        self._parse_devices()
        await self._load_keystore_addresses()

    async def end_session(self, address: str = None):
        """Disconnect one session by address, or all when address is None."""
        if address is None:
            targets = list(self.sessions.values())
        else:
            session = self.sessions.get(normalize_addr(address))
            targets = [session] if session else []
        if not targets:
            log.info("No matching connection to disconnect")
            return
        for session in targets:
            await self._teardown_session(session)

    # ==================== PAIRING ====================

    async def pair_device(self, address: str, protocol: Protocol = None, name: str = None) -> bool:
        """Pair with a device (first-time setup)."""
        if protocol is None:
            protocol = Protocol.BLE

        self._parse_devices()

        bucket = self.classic_devices if protocol == Protocol.CLASSIC else self.ble_devices
        norm = normalize_addr(address)
        if not any(d.address != '*' and normalize_addr(d.address) == norm for d in bucket):
            bucket.append(DeviceConfig(address=address, protocol=protocol, name=name))

        await self.start(pairing=True)

        if protocol == Protocol.CLASSIC:
            return await self._pair_classic(address)
        else:
            return await self._pair_ble(address)

    async def continue_after_pairing(self):
        """Serve all configured devices, adopting the paired session."""
        session = self._pairing_session
        if not session:
            raise InvalidStateError("No paired device - call pair_device first")
        self._pairing_session = None

        self._sessions_changed = asyncio.Event()
        self._radio_lock = asyncio.Lock()

        if session.is_alive():
            self._register_session(session)
            if session.protocol == Protocol.CLASSIC:
                await self._continue_classic_after_pairing(session)
            else:
                await self._continue_ble_after_pairing(session)
            proto_name = session.protocol.value.upper()
            log.success(f"\n[{proto_name}] Paired and receiving HID reports.")
        else:
            log.info("Paired device disconnected; the connect loops will pick it up")

        self._parse_devices()
        await self._load_keystore_addresses()
        await self._serve()

    # ==================== COMMON ====================

    def _forward_report(self, session: DeviceSession, data: bytes):
        """Deduplicate the log line and forward an HID report to UHID."""
        if data != session.last_report:
            log.debug(f"Report: {data.hex()}")
            session.last_report = data
        if session.uhid_device:
            try:
                session.uhid_device.send_input(data)
            except Exception as e:
                log.warning(f"UHID send failed: {e}")

    def _load_cached_descriptor(self, session: DeviceSession) -> bool:
        """Load report descriptor and device name from cache. Returns True if found."""
        cache = self.device_cache.load(session.address) or \
            self.device_cache.load(session.raw_address)
        if cache and 'report_map' in cache:
            session.report_map = bytes.fromhex(cache['report_map'])
            session.name = clean_device_name(cache.get('device_name') or '') or None
            log.success(f"Loaded cached descriptor ({len(session.report_map)} bytes)")
            return True
        return False

    def _create_uhid_device(self, session: DeviceSession):
        """Create UHID virtual device."""
        if not session.report_map:
            log.warning("No report descriptor for UHID")
            return

        if not ensure_uhid():
            log.error("uhid unavailable; connected device will have no input path")
            return

        try:
            name = self._configured_name(session.address) or session.name or "HID Device"
            descriptor = strip_digitizer_collections(session.report_map)
            session.uhid_device = UHIDDevice(
                name=name,
                report_descriptor=descriptor,
                bus=Bus.BLUETOOTH,
                vendor=0,
                product=0,
                uniq=session.address,
            )
            log.success(f"UHID device created: {name}")
            asyncio.get_event_loop().call_later(
                0.5, session.uhid_device.discover_input_paths)
            session.is_pointer = descriptor_is_pointer(descriptor)
            if session.is_pointer:
                log.info("Pointer device: cursor overlay on")
                if self._pointer_count() == 1:
                    self._notify_pointer(True)
        except Exception as e:
            log.error(f"Failed to create UHID device: {e}")

    def _pointer_count(self) -> int:
        return sum(1 for s in self.sessions.values()
                   if s.is_pointer and s.uhid_device)

    def _notify_pointer(self, active: bool):
        """Tell the daemon a pointer device appeared/left, so it can toggle the cursor."""
        if not self.on_pointer_change:
            return
        try:
            self.on_pointer_change(active)
        except Exception as e:
            log.warning(f"cursor hook failed: {e}")

    async def cleanup(self):
        """Clean up resources."""
        if self._connection_tasks:
            pending = list(self._connection_tasks)
            for task in pending:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass
            self._connection_tasks.clear()

        had_pointer = self._pointer_count() > 0
        for session in list(self.sessions.values()):
            self.sessions.pop(session.address, None)
            try:
                await session.cleanup()
            except Exception:
                pass
        if had_pointer:
            self._notify_pointer(False)

        if self._pairing_session:
            try:
                await self._pairing_session.cleanup()
            except Exception:
                pass
            self._pairing_session = None

        if hasattr(self, '_classic_connection_listener') and self._classic_connection_listener:
            try:
                self.device.remove_listener('connection', self._classic_connection_listener)
            except Exception:
                pass
            self._classic_connection_listener = None

        if self.transport:
            try:
                await asyncio.wait_for(self.transport.close(), timeout=3.0)
            except asyncio.TimeoutError:
                log.warning("Transport close timed out, fd may leak")
            except Exception:
                pass
            self.transport = None
            from bt_setup import chip
            chip().on_transport_close()


Microsoft Windows [版本 10.0.19045.6466]
(c) Microsoft Corporation。保留所有权利。

C:\Users\AQ\Downloads>scp kindle-hid-passthrough-armv7_dev.tar.gz root@192.168.0.104:/mnt/us/
root@192.168.0.104's password:
kindle-hid-passthrough-armv7_dev.tar.gz                                               100%   23MB   2.1MB/s   00:11

C:\Users\AQ\Downloads>ssh root@192.168.0.104
root@192.168.0.104's password:
#################################################
#  N O T I C E  *  N O T I C E  *  N O T I C E  #
#################################################
Rootfs is mounted read-only. Invoke mntroot rw to
switch back to a writable rootfs.
#################################################
[root@kindle us]# tar -xzf kindle-hid-passthrough-armv7_dev.tar.gz -C /mnt/us/khp-release-dev
[root@kindle us]# sh /mnt/us/khp-release-dev/scripts/install.sh update

=== Install / Update ===
Existing install found at /mnt/us/kindle_hid_passthrough, updating it in place.
 -> Stopping daemon
hid-passthrough stop/waiting
 -> Installing main program files
 -> Kept your config.ini, new defaults written to config.ini.new
 -> Ready.
 -> Installing udev rules
system: I mntroot:def:Making root filesystem writeable
system: I mntroot:def:Making root filesystem read-only
 -> Ready.
 -> Installing upstart service
system: I mntroot:def:Making root filesystem writeable
system: I mntroot:def:Making root filesystem read-only
 -> Ready.
 -> Installing BTManager app

=== BTManager Installer ===

1. Checking WAF app files...
   Files at /mnt/us/kindle_hid_passthrough/illusion/BTManager
2. Setting permissions...
   Done
3. Purging stale WAF cache...
   Done
4. Registering app...
   Registered com.lzampier.btmanager in appreg.db
5. Installing scriptlet...
   Installed to /mnt/us/documents/BTManager.sh
6. Starting daemon...
   Daemon started via upstart

=== Installation Complete ===

You can now:
  - Open 'BT Manager' from the Kindle library (scriptlet)
  - Or launch directly: lipc-set-prop com.lab126.appmgrd start app://com.lzampier.btmanager

To start on boot, install the upstart config:
  cp /mnt/us/kindle_hid_passthrough/illusion/../assets/hid-passthrough.upstart /etc/upstart/hid-passthrough.conf

 -> Installing Button Mapper
system: I mntroot:def:Making root filesystem writeable

=== MapperManager Installer ===

1. App files at /mnt/us/kindle-button-mapper/illusion/MapperManager
2. Setting scriptlet permissions
3. Registering app
   Already registered
4. Installing scriptlet
   Installed at /mnt/us/documents/MapperManager.sh

=== Installation Complete ===

Open 'Button Mapper' (MapperManager.sh) from the Kindle library, or run:
  lipc-set-prop com.lab126.appmgrd start app://com.lzampier.mappermanager

system: I mntroot:def:Making root filesystem read-only
kindle-button-mapper start/running, process 17647
Installed. Open Button Mapper from the Kindle library or via:
  lipc-set-prop com.lab126.appmgrd start app://com.lzampier.mappermanager
 -> Ready.
 -> Installing KOReader plugin into /mnt/us/koreader/plugins
 -> Ready. Restart KOReader to load it.

Installation complete. Open 'BT Manager' from the Kindle library.
[root@kindle us]# /sbin/stop hid-passthrough
hid-passthrough stop/waiting
[root@kindle us]# pkill -f "ld-linux-armhf" 2>/dev/null
[root@kindle us]# /sbin/start hid-passthrough
hid-passthrough start/running, process 17685
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [], "ok": true, "version": "3.14.1-a38e6e0", "autostart": true}[root@kindle us]# tail -40 /var/log/hid_passthrough.log
2026-08-21 23:35:05,014 INFO daemon: === Starting connection ===
2026-08-21 23:35:05,019 INFO ble_hid: Devices: 0 Classic, 1 BLE
2026-08-21 23:35:05,019 INFO ble_hid: HID Host v3.14.1-a38e6e0
2026-08-21 23:35:05,020 INFO ble_hid: Opening transport...
2026-08-21 23:35:05,098 INFO ble_hid: Sending HCI Reset...
2026-08-21 23:35:05,296 INFO ble_hid: HCI Reset successful
2026-08-21 23:35:05,818 INFO ble_hid: Device powered on: 00:00:46:67:61:01/P
2026-08-21 23:35:05,838 INFO ble_hid: Controller address resolution enabled
2026-08-21 23:35:05,839 INFO ble_hid: BLE enabled
2026-08-21 23:35:05,852 INFO ble_hid: Keystore has 1 entries
2026-08-21 23:35:05,858 INFO device_cache: Loaded device cache for EA:F5:FD:0F:A4:11
2026-08-21 23:35:05,859 INFO ble_hid: Cached descriptor for Free 2 (EA:F5:FD:0F:A4:11)
2026-08-21 23:35:05,860 INFO ble_hid: Serving devices (Classic: 0, BLE: 1)
2026-08-21 23:35:05,864 INFO ble_hid: [BLE] Accept-list handler running
2026-08-21 23:35:37,130 INFO daemon: Shutdown signal received
2026-08-21 23:35:37,132 INFO daemon: Stopping...
2026-08-21 23:35:37,136 WARNING ble_hid: lipc-wait-event exited (-15); respawning
2026-08-21 23:36:09,301 INFO ble_hid: Preparing Bluetooth hardware...
2026-08-21 23:36:09,303 INFO ble_hid: Detected Kindle PW6 (code 0xC7F)
2026-08-21 23:36:09,304 INFO ble_hid: BT module already loaded: wmt_cdev_bt.ko
2026-08-21 23:36:09,361 INFO ble_hid: /dev/stpbt is available
2026-08-21 23:36:09,400 INFO ble_hid: API server listening on port 8321
2026-08-21 23:36:09,403 INFO ble_hid: Watching powerd for system suspend
2026-08-21 23:36:09,407 INFO ble_hid: Kindle HID Passthrough v3.14.1-a38e6e0 (daemon)
2026-08-21 23:36:09,410 INFO daemon: HID Daemon v3.14.1-a38e6e0
2026-08-21 23:36:09,414 INFO daemon: Device: Free 2 (EA:F5:FD:0F:A4:11) (ble)
2026-08-21 23:36:09,414 INFO daemon: === Starting connection ===
2026-08-21 23:36:09,419 INFO ble_hid: Devices: 0 Classic, 1 BLE
2026-08-21 23:36:09,419 INFO ble_hid: HID Host v3.14.1-a38e6e0
2026-08-21 23:36:09,420 INFO ble_hid: Opening transport...
2026-08-21 23:36:09,498 INFO ble_hid: Sending HCI Reset...
2026-08-21 23:36:09,662 INFO ble_hid: HCI Reset successful
2026-08-21 23:36:10,128 INFO ble_hid: Device powered on: 00:00:46:67:61:01/P
2026-08-21 23:36:10,143 INFO ble_hid: Controller address resolution enabled
2026-08-21 23:36:10,144 INFO ble_hid: BLE enabled
2026-08-21 23:36:10,147 INFO ble_hid: Keystore has 1 entries
2026-08-21 23:36:10,150 INFO device_cache: Loaded device cache for EA:F5:FD:0F:A4:11
2026-08-21 23:36:10,151 INFO ble_hid: Cached descriptor for Free 2 (EA:F5:FD:0F:A4:11)
2026-08-21 23:36:10,152 INFO ble_hid: Serving devices (Classic: 0, BLE: 1)
2026-08-21 23:36:10,153 INFO ble_hid: [BLE] Accept-list handler running
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2", "hid_ready": true, "uhid_name": "Free 2", "input_paths": ["/dev/input/event3"], "descriptor_size": 220}], "ok": true, "version": "3.14.1-a38e6e0", "autostart": true}[root@kindle us]# tail -40 /var/log/hid_passthrough.log
2026-08-21 23:37:15,233 INFO ble_hid: HID Host v3.14.1-a38e6e0
2026-08-21 23:37:15,234 INFO ble_hid: Opening transport...
2026-08-21 23:37:15,298 INFO ble_hid: Sending HCI Reset...
2026-08-21 23:37:15,486 INFO ble_hid: HCI Reset successful
2026-08-21 23:37:15,955 INFO ble_hid: Device powered on: 00:00:46:67:61:01/P
2026-08-21 23:37:15,970 INFO ble_hid: Controller address resolution enabled
2026-08-21 23:37:15,971 INFO ble_hid: BLE enabled
2026-08-21 23:37:15,975 INFO ble_hid: Keystore has 1 entries
2026-08-21 23:37:15,977 INFO device_cache: Loaded device cache for EA:F5:FD:0F:A4:11
2026-08-21 23:37:15,978 INFO ble_hid: Cached descriptor for Free 2 (EA:F5:FD:0F:A4:11)
2026-08-21 23:37:15,979 INFO ble_hid: Serving devices (Classic: 0, BLE: 1)
2026-08-21 23:37:15,980 INFO ble_hid: [BLE] Accept-list handler running
2026-08-21 23:37:53,477 INFO ble_hid: [BLE] Device connected: Free 2 (EA:F5:FD:0F:A4:11)
2026-08-21 23:37:53,488 INFO ble_hid: [BLE] Restoring bonding...
2026-08-21 23:37:53,626 INFO ble_hid: [BLE] Bonding restored
2026-08-21 23:37:53,630 INFO device_cache: Loaded device cache for EA:F5:FD:0F:A4:11
2026-08-21 23:37:53,631 INFO ble_hid: Loaded cached descriptor (220 bytes)
2026-08-21 23:37:53,773 INFO ble_hid: [BLE] Found HID service
2026-08-21 23:37:54,184 INFO ble_hid: [BLE] Found input report 1
2026-08-21 23:37:54,710 INFO ble_hid: [BLE] Found input report 2
2026-08-21 23:37:54,813 INFO ble_hid: [BLE] Found input report 3
2026-08-21 23:37:54,918 INFO ble_hid: [BLE] Found input report 4
2026-08-21 23:37:55,023 INFO ble_hid: [BLE] Found input report 5
2026-08-21 23:37:55,129 INFO ble_hid: [BLE] Found input report 6
2026-08-21 23:37:55,131 INFO uhid_handler: Stripped digitizer collection(s) (220 -> 146 bytes)
2026-08-21 23:37:55,134 INFO uhid_handler: Created UHID device: Free 2 (vendor=0x0000, product=0x0000, rd_size=146)
2026-08-21 23:37:55,135 INFO ble_hid: UHID device created: Free 2
2026-08-21 23:37:55,234 INFO ble_hid: [BLE] Subscribed to report 1
2026-08-21 23:37:55,339 INFO ble_hid: [BLE] Subscribed to report 2
2026-08-21 23:37:55,445 INFO ble_hid: [BLE] Subscribed to report 3
2026-08-21 23:37:55,549 INFO ble_hid: [BLE] Subscribed to report 4
2026-08-21 23:37:55,653 INFO ble_hid: [BLE] Subscribed to report 5
2026-08-21 23:37:55,759 INFO ble_hid: [BLE] Subscribed to report 6
2026-08-21 23:37:55,762 INFO ble_hid: [BLE] Wrote Exit Suspend to HID Control Point
2026-08-21 23:37:55,812 INFO ble_hid: [BLE] Protocol Mode: Report
2026-08-21 23:37:55,813 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) receiving HID reports
2026-08-21 23:37:56,075 INFO ble_hid: [BLE] No Battery Service on Free 2 (EA:F5:FD:0F:A4:11); services: ['0018', '0f18', '30ae', '1218', '0a18', '00ae']
2026-08-21 23:37:56,284 INFO ble_hid: [BLE] Subscribed to notify 192a on EA:F5:FD:0F:A4:11
2026-08-21 23:37:56,494 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-21 23:37:56,914 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
[root@kindle us]# tail -30 /var/log/hid_passthrough.log
2026-08-21 23:37:53,488 INFO ble_hid: [BLE] Restoring bonding...
2026-08-21 23:37:53,626 INFO ble_hid: [BLE] Bonding restored
2026-08-21 23:37:53,630 INFO device_cache: Loaded device cache for EA:F5:FD:0F:A4:11
2026-08-21 23:37:53,631 INFO ble_hid: Loaded cached descriptor (220 bytes)
2026-08-21 23:37:53,773 INFO ble_hid: [BLE] Found HID service
2026-08-21 23:37:54,184 INFO ble_hid: [BLE] Found input report 1
2026-08-21 23:37:54,710 INFO ble_hid: [BLE] Found input report 2
2026-08-21 23:37:54,813 INFO ble_hid: [BLE] Found input report 3
2026-08-21 23:37:54,918 INFO ble_hid: [BLE] Found input report 4
2026-08-21 23:37:55,023 INFO ble_hid: [BLE] Found input report 5
2026-08-21 23:37:55,129 INFO ble_hid: [BLE] Found input report 6
2026-08-21 23:37:55,131 INFO uhid_handler: Stripped digitizer collection(s) (220 -> 146 bytes)
2026-08-21 23:37:55,134 INFO uhid_handler: Created UHID device: Free 2 (vendor=0x0000, product=0x0000, rd_size=146)
2026-08-21 23:37:55,135 INFO ble_hid: UHID device created: Free 2
2026-08-21 23:37:55,234 INFO ble_hid: [BLE] Subscribed to report 1
2026-08-21 23:37:55,339 INFO ble_hid: [BLE] Subscribed to report 2
2026-08-21 23:37:55,445 INFO ble_hid: [BLE] Subscribed to report 3
2026-08-21 23:37:55,549 INFO ble_hid: [BLE] Subscribed to report 4
2026-08-21 23:37:55,653 INFO ble_hid: [BLE] Subscribed to report 5
2026-08-21 23:37:55,759 INFO ble_hid: [BLE] Subscribed to report 6
2026-08-21 23:37:55,762 INFO ble_hid: [BLE] Wrote Exit Suspend to HID Control Point
2026-08-21 23:37:55,812 INFO ble_hid: [BLE] Protocol Mode: Report
2026-08-21 23:37:55,813 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) receiving HID reports
2026-08-21 23:37:56,075 INFO ble_hid: [BLE] No Battery Service on Free 2 (EA:F5:FD:0F:A4:11); services: ['0018', '0f18', '30ae', '1218', '0a18', '00ae']
2026-08-21 23:37:56,284 INFO ble_hid: [BLE] Subscribed to notify 192a on EA:F5:FD:0F:A4:11
2026-08-21 23:37:56,494 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-21 23:37:56,914 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-21 23:38:56,976 INFO ble_hid: [BLE] Notify 192a: 5b
2026-08-21 23:38:56,977 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) battery: 91% (notify 192a)
2026-08-21 23:42:16,138 INFO ble_hid: [BLE] No Battery Service on Free 2 (EA:F5:FD:0F:A4:11); services: ['0018', '0f18', '30ae', '1218', '0a18', '00ae']
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2", "hid_ready": true, "uhid_name": "Free 2", "input_paths": ["/dev/input/event3"], "descriptor_size": 220, "battery_level": 91, "battery_updated": 1787326736.976716}], "ok": true, "version": "3.14.1-a38e6e0", "autostart": true}[root@kindle us]# ls /sys/class/net/
ifb0   ifb1   lo     sit0   tunl0  usb0   wlan0
[root@kindle us]# /mnt/us/usbnetlite/bin/usbnetwork usbms
wall: can't open 'We're switching back to USB MS, so if you're wondering why your terminal is frozen, go read the docs!': No such file or directory
[root@kindle us]# Connection to 192.168.0.104 closed by remote host.
Connection to 192.168.0.104 closed.

C:\Users\AQ\Downloads>ssh root@192.168.0.104
root@192.168.0.104's password:
#################################################
#  N O T I C E  *  N O T I C E  *  N O T I C E  #
#################################################
Rootfs is mounted read-only. Invoke mntroot rw to
switch back to a writable rootfs.
#################################################
[root@kindle us]# ls /mnt/us/koreader/plugins/
SSH.koplugin                 cloudlibrary.koplugin        httpinspector.koplugin       simpleui.koplugin
archiveviewer.koplugin       cloudstorage.koplugin        japanese.koplugin            statistics.koplugin
autodim.koplugin             coverbrowser.koplugin        keepalive.koplugin           systemstat.koplugin
autostandby.koplugin         coverimage.koplugin          kosync.koplugin              terminal.koplugin
autosuspend.koplugin         docsettingtweak.koplugin     movetoarchive.koplugin       texteditor.koplugin
autoturn.koplugin            exporter.koplugin            newsdownloader.koplugin      timesync.koplugin
autowarmth.koplugin          externalkeyboard.koplugin    opds.koplugin                vocabbuilder.koplugin
batterystat.koplugin         filebrowserplus.koplugin     perceptionexpander.koplugin  wallabag.koplugin
bookends.koplugin            gestures.koplugin            pinyin_enhancement.koplugin  zlibrary.koplugin
bookshortcuts.koplugin       hello.koplugin               profiles.koplugin
btbattery.koplugin           hidpassthrough.koplugin      qrclipboard.koplugin
calibre.koplugin             hotkeys.koplugin             readtimer.koplugin
[root@kindle us]# head -8 /mnt/us/koreader/plugins/bookends.koplugin/_meta.lua
local _ = require("i18n").gettext
return {
    name = "bookends",
    fullname = _("Bookends"),
    description = _([[Configurable text overlays at screen corners and edges with token expansion and icon support.]]),
    version = "3.3.1",
}
[root@kindle us]# client_loop: send disconnect: Connection reset

C:\Users\AQ\Downloads>ssh root@192.168.0.104
root@192.168.0.104's password:
#################################################
#  N O T I C E  *  N O T I C E  *  N O T I C E  #
#################################################
Rootfs is mounted read-only. Invoke mntroot rw to
switch back to a writable rootfs.
#################################################
[root@kindle us]# curl -s http://127.0.0.1:8321/status
[root@kindle us]# ls /mnt/us/koreader/plugins/ | grep -iE "bookends|btbattery"
bookends.koplugin
btbattery.koplugin
[root@kindle us]# head -8 /mnt/us/koreader/plugins/bookends.koplugin/_meta.lua
local _ = require("bookends_i18n").gettext
return {
    -- KEEP `name`, equal to the .koplugin directory id ("bookends"). Do not
    -- remove again, even though current KOReader deprecates it (koreader#15096:
    -- nightly logs a harmless "name in _meta.lua is deprecated" WARN and keys
    -- enable/disable off the directory id instead).
    --
    -- Why it's load-bearing on stable releases (confirmed v2025.10, before the
[root@kindle us]# head -8 /mnt/us/koreader/plugins/bookends.koplugin/_meta.lua
local _ = require("bookends_i18n").gettext
return {
    -- KEEP `name`, equal to the .koplugin directory id ("bookends"). Do not
    -- remove again, even though current KOReader deprecates it (koreader#15096:
    -- nightly logs a harmless "name in _meta.lua is deprecated" WARN and keys
    -- enable/disable off the directory id instead).
    --
    -- Why it's load-bearing on stable releases (confirmed v2025.10, before the
[root@kindle us]# grep -iE "btbattery|bookends|error" /mnt/us/koreader/crash.log | tail -30
08/21/26-20:20:50 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 274 strings
08/21/26-20:20:50 INFO  bookends i18n: installed for language: zh_CN
08/21/26-20:20:50 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/21/26-20:20:51 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
08/21/26-20:29:15 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 274 strings
08/21/26-20:29:15 INFO  bookends i18n: installed for language: zh_CN
08/21/26-20:29:15 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/21/26-20:29:17 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
08/21/26-23:50:46 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 274 strings
08/21/26-23:50:46 INFO  bookends i18n: installed for language: zh_CN
08/21/26-23:50:46 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/21/26-23:50:48 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
08/21/26-23:50:59 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 274 strings
08/21/26-23:50:59 INFO  bookends i18n: installed for language: zh_CN
08/21/26-23:50:59 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/21/26-23:51:00 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:02:40 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 567 strings
08/22/26-00:02:40 INFO  bookends i18n: installed for language: zh_CN
08/22/26-00:02:40 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/22/26-00:02:42 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
08/22/26-00:02:51 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 567 strings
08/22/26-00:02:51 INFO  bookends i18n: installed for language: zh_CN
08/22/26-00:02:51 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/22/26-00:02:52 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
08/22/26-00:03:01 INFO  bookends: migrated active settings bar_colors → per-bar
08/22/26-00:11:15 INFO  bookends i18n: loaded plugins/bookends.koplugin/locale/zh_CN.po — 567 strings
08/22/26-00:11:15 INFO  bookends i18n: installed for language: zh_CN
08/22/26-00:11:15 WARN  PluginLoader: bookends name in _meta.lua, is deprecated and will be ignored.
08/22/26-00:11:17 ERROR failed to register crengine font: cannot register font </mnt/us/fonts/AI楷.ttf>
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2", "hid_ready": true, "uhid_name": "Free 2", "input_paths": ["/dev/input/event2"], "descriptor_size": 220}], "ok": true, "version": "3.14.1-a38e6e0", "autostart": true}[root@kindle us]# client_loop: send disconnect: Connection reset

C:\Users\AQ\Downloads>ssh root@192.168.0.104
ssh: connect to host 192.168.0.104 port 22: Connection refused

C:\Users\AQ\Downloads>ssh root@192.168.0.104
ssh: connect to host 192.168.0.104 port 22: Connection refused

C:\Users\AQ\Downloads>ssh root@192.168.0.104
root@192.168.0.104's password:
#################################################
#  N O T I C E  *  N O T I C E  *  N O T I C E  #
#################################################
Rootfs is mounted read-only. Invoke mntroot rw to
switch back to a writable rootfs.
#################################################
[root@kindle us]# tail -25 /var/log/hid_passthrough.log
2026-08-22 00:22:58,211 INFO device_cache: Loaded device cache for EA:F5:FD:0F:A4:11
2026-08-22 00:22:58,212 INFO ble_hid: Loaded cached descriptor (220 bytes)
2026-08-22 00:22:58,353 INFO ble_hid: [BLE] Found HID service
2026-08-22 00:22:58,779 INFO ble_hid: [BLE] Found input report 1
2026-08-22 00:22:58,989 INFO ble_hid: [BLE] Found input report 2
2026-08-22 00:22:59,095 INFO ble_hid: [BLE] Found input report 3
2026-08-22 00:22:59,200 INFO ble_hid: [BLE] Found input report 4
2026-08-22 00:22:59,304 INFO ble_hid: [BLE] Found input report 5
2026-08-22 00:22:59,410 INFO ble_hid: [BLE] Found input report 6
2026-08-22 00:22:59,412 INFO uhid_handler: Stripped digitizer collection(s) (220 -> 146 bytes)
2026-08-22 00:22:59,418 INFO uhid_handler: Created UHID device: Free 2 (vendor=0x0000, product=0x0000, rd_size=146)
2026-08-22 00:22:59,419 INFO ble_hid: UHID device created: Free 2
2026-08-22 00:22:59,514 INFO ble_hid: [BLE] Subscribed to report 1
2026-08-22 00:22:59,619 INFO ble_hid: [BLE] Subscribed to report 2
2026-08-22 00:22:59,724 INFO ble_hid: [BLE] Subscribed to report 3
2026-08-22 00:22:59,829 INFO ble_hid: [BLE] Subscribed to report 4
2026-08-22 00:22:59,934 INFO ble_hid: [BLE] Subscribed to report 5
2026-08-22 00:23:00,038 INFO ble_hid: [BLE] Subscribed to report 6
2026-08-22 00:23:00,042 INFO ble_hid: [BLE] Wrote Exit Suspend to HID Control Point
2026-08-22 00:23:00,091 INFO ble_hid: [BLE] Protocol Mode: Report
2026-08-22 00:23:00,104 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) receiving HID reports
2026-08-22 00:23:00,352 INFO ble_hid: [BLE] No Battery Service on Free 2 (EA:F5:FD:0F:A4:11); services: ['0018', '0f18', '30ae', '1218', '0a18', '00ae']
2026-08-22 00:23:00,564 INFO ble_hid: [BLE] Subscribed to notify 192a on EA:F5:FD:0F:A4:11
2026-08-22 00:23:00,774 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-22 00:23:01,194 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
[root@kindle us]# tail -25 /var/log/hid_passthrough.log
2026-08-22 00:22:58,353 INFO ble_hid: [BLE] Found HID service
2026-08-22 00:22:58,779 INFO ble_hid: [BLE] Found input report 1
2026-08-22 00:22:58,989 INFO ble_hid: [BLE] Found input report 2
2026-08-22 00:22:59,095 INFO ble_hid: [BLE] Found input report 3
2026-08-22 00:22:59,200 INFO ble_hid: [BLE] Found input report 4
2026-08-22 00:22:59,304 INFO ble_hid: [BLE] Found input report 5
2026-08-22 00:22:59,410 INFO ble_hid: [BLE] Found input report 6
2026-08-22 00:22:59,412 INFO uhid_handler: Stripped digitizer collection(s) (220 -> 146 bytes)
2026-08-22 00:22:59,418 INFO uhid_handler: Created UHID device: Free 2 (vendor=0x0000, product=0x0000, rd_size=146)
2026-08-22 00:22:59,419 INFO ble_hid: UHID device created: Free 2
2026-08-22 00:22:59,514 INFO ble_hid: [BLE] Subscribed to report 1
2026-08-22 00:22:59,619 INFO ble_hid: [BLE] Subscribed to report 2
2026-08-22 00:22:59,724 INFO ble_hid: [BLE] Subscribed to report 3
2026-08-22 00:22:59,829 INFO ble_hid: [BLE] Subscribed to report 4
2026-08-22 00:22:59,934 INFO ble_hid: [BLE] Subscribed to report 5
2026-08-22 00:23:00,038 INFO ble_hid: [BLE] Subscribed to report 6
2026-08-22 00:23:00,042 INFO ble_hid: [BLE] Wrote Exit Suspend to HID Control Point
2026-08-22 00:23:00,091 INFO ble_hid: [BLE] Protocol Mode: Report
2026-08-22 00:23:00,104 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) receiving HID reports
2026-08-22 00:23:00,352 INFO ble_hid: [BLE] No Battery Service on Free 2 (EA:F5:FD:0F:A4:11); services: ['0018', '0f18', '30ae', '1218', '0a18', '00ae']
2026-08-22 00:23:00,564 INFO ble_hid: [BLE] Subscribed to notify 192a on EA:F5:FD:0F:A4:11
2026-08-22 00:23:00,774 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-22 00:23:01,194 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-22 00:24:01,543 INFO ble_hid: [BLE] Notify 192a: 57
2026-08-22 00:24:01,544 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) battery: 87% (notify 192a)
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2", "hid_ready": true, "uhid_name": "Free 2", "input_paths": ["/dev/input/event3"], "descriptor_size": 220, "battery_level": 88, "battery_updated": 1787329505.6747282}], "ok": true, "version": "3.14.1-a38e6e0", "autostart": true}[root@kindle us]# tail -25 /var/log/hid_passthrough.log
2026-08-22 00:22:58,989 INFO ble_hid: [BLE] Found input report 2
2026-08-22 00:22:59,095 INFO ble_hid: [BLE] Found input report 3
2026-08-22 00:22:59,200 INFO ble_hid: [BLE] Found input report 4
2026-08-22 00:22:59,304 INFO ble_hid: [BLE] Found input report 5
2026-08-22 00:22:59,410 INFO ble_hid: [BLE] Found input report 6
2026-08-22 00:22:59,412 INFO uhid_handler: Stripped digitizer collection(s) (220 -> 146 bytes)
2026-08-22 00:22:59,418 INFO uhid_handler: Created UHID device: Free 2 (vendor=0x0000, product=0x0000, rd_size=146)
2026-08-22 00:22:59,419 INFO ble_hid: UHID device created: Free 2
2026-08-22 00:22:59,514 INFO ble_hid: [BLE] Subscribed to report 1
2026-08-22 00:22:59,619 INFO ble_hid: [BLE] Subscribed to report 2
2026-08-22 00:22:59,724 INFO ble_hid: [BLE] Subscribed to report 3
2026-08-22 00:22:59,829 INFO ble_hid: [BLE] Subscribed to report 4
2026-08-22 00:22:59,934 INFO ble_hid: [BLE] Subscribed to report 5
2026-08-22 00:23:00,038 INFO ble_hid: [BLE] Subscribed to report 6
2026-08-22 00:23:00,042 INFO ble_hid: [BLE] Wrote Exit Suspend to HID Control Point
2026-08-22 00:23:00,091 INFO ble_hid: [BLE] Protocol Mode: Report
2026-08-22 00:23:00,104 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) receiving HID reports
2026-08-22 00:23:00,352 INFO ble_hid: [BLE] No Battery Service on Free 2 (EA:F5:FD:0F:A4:11); services: ['0018', '0f18', '30ae', '1218', '0a18', '00ae']
2026-08-22 00:23:00,564 INFO ble_hid: [BLE] Subscribed to notify 192a on EA:F5:FD:0F:A4:11
2026-08-22 00:23:00,774 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-22 00:23:01,194 INFO ble_hid: [BLE] Subscribed to notify 02ae on EA:F5:FD:0F:A4:11
2026-08-22 00:24:01,543 INFO ble_hid: [BLE] Notify 192a: 57
2026-08-22 00:24:01,544 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) battery: 87% (notify 192a)
2026-08-22 00:25:05,674 INFO ble_hid: [BLE] Notify 192a: 58
2026-08-22 00:25:05,675 INFO ble_hid: [BLE] Free 2 (EA:F5:FD:0F:A4:11) battery: 88% (notify 192a)
[root@kindle us]# killall reader.lua
killall: reader.lua: no process killed
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2", "hid_ready": true, "uhid_name": "Free 2", "input_paths": ["/dev/input/event3"], "descriptor_size": 220, "battery_level": 87, "battery_updated": 1787330170.99141}], "ok": true, "version": "3.14.1-a38e6e0",[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
[root@kindle us]# curl -s http://127.0.0.1:8321/status
{"daemon_running": true, "devices": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2"}], "device_count": 1, "scanning": false, "pairing": false, "cursor_running": false, "connections": [{"address": "EA:F5:FD:0F:A4:11", "protocol": "ble", "name": "Free 2", "hid_ready": true, "uhid_name": "Free 2", "input_paths": ["/dev/input/event3"], "descriptor_size": 220, "battery_level": 87, "battery_updated": 1787330170.99141}], "ok": true, "version": "3.14.1-a38e6e0", "autostart": true}[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: battery is now 87%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: battery is now 87%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: battery is now 87%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/21/26-23:51:06 INFO  btbattery: battery is now 90%
08/22/26-00:24:44 INFO  btbattery: battery is now 87%
08/22/26-00:27:35 INFO  btbattery: battery is now 88%
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: battery is now 87%
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: battery is now 86%
[root@kindle us]# cat /mnt/us/koreader/git-rev 2>/dev/null
v2026.07.1
[root@kindle us]# cat /mnt/us/koreader/koreader/git-rev 2>/dev/null
[root@kindle us]# ls /mnt/us/koreader/ | head
COPYING
README.md
books
cache
clipboard
cloudlibrary_sync_log.txt
common
crash.log
data
datastorage.lua
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/22/26-00:33:49 INFO  btbattery: polled, no battery data yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: footer not available yet
08/22/26-00:45:52 INFO  btbattery: battery is now 87%
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: battery is now 86%
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: battery is now 86%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: battery is now 86%
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: battery is now 86%
08/22/26-00:55:47 INFO  btbattery: footer callback registered
08/22/26-00:55:47 INFO  btbattery: battery is now 86%
08/22/26-00:59:21 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: battery is now 86%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: battery is now 86%
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: battery is now 86%
08/22/26-00:55:47 INFO  btbattery: footer callback registered
08/22/26-00:55:47 INFO  btbattery: battery is now 86%
08/22/26-00:59:21 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: battery is now 86%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: battery is now 86%
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: battery is now 86%
08/22/26-00:55:47 INFO  btbattery: footer callback registered
08/22/26-00:55:47 INFO  btbattery: battery is now 86%
08/22/26-00:59:21 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: battery is now 86%
[root@kindle us]# grep -i "btbattery" /mnt/us/koreader/crash.log | tail -10
08/22/26-00:48:33 INFO  btbattery: footer not available yet
08/22/26-00:48:33 INFO  btbattery: battery is now 86%
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: footer not found (ui.view=nil, view.footer=nil, ui.footer=nil)
08/22/26-00:52:10 INFO  btbattery: battery is now 86%
08/22/26-00:55:47 INFO  btbattery: footer callback registered
08/22/26-00:55:47 INFO  btbattery: battery is now 86%
08/22/26-00:59:21 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: footer callback registered
08/22/26-01:02:20 INFO  btbattery: battery is now 86%
[root@kindle us]#