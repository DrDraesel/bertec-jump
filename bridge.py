"""Windows x64 ctypes bridge to the official Bertec Device SDK 2.57.
ABI source: vendor/bertecif.h. No USB protocol guessing or synthetic data.
All methods must be called on one acquisition thread. Polling keeps Python
work outside vendor callbacks; a 5000-frame vendor buffer absorbs short stalls.
"""
import ctypes as c
import math
import os
import time
from pathlib import Path


class DeviceInfo(c.Structure):
    _pack_ = 8
    _fields_ = [('fwVersion', c.c_ushort), ('prVersionMajor', c.c_ubyte),
                ('prVersionMinor', c.c_ubyte), ('status', c.c_int),
                ('samplingFreq', c.c_ushort), ('hwid', c.c_ushort),
                ('serial', c.c_char * 32), ('model', c.c_char * 32),
                ('channelCount', c.c_int), ('has64BitTimestamp', c.c_bool),
                ('hasAuxSyncPins', c.c_bool), ('dimensionWidth', c.c_float),
                ('dimensionHeight', c.c_float), ('offsetX', c.c_float),
                ('offsetY', c.c_float), ('offsetZ', c.c_float),
                ('channelNames', (c.c_char * 16) * 32)]


class ChannelData(c.Structure):
    _fields_ = [('count', c.c_int), ('data', c.c_float * 32)]


class AdditionalData(c.Structure):
    _pack_ = 8
    _fields_ = [('timestamp', c.c_uint64), ('eventCounter', c.c_uint64),
                ('frameCounter', c.c_uint64), ('auxData', c.c_ubyte), ('syncData', c.c_ubyte)]


class DeviceData(c.Structure):
    _pack_ = 8
    _fields_ = [('channelData', ChannelData), ('additionalData', AdditionalData)]


class Frame(c.Structure):
    _pack_ = 8
    _fields_ = [('deviceCount', c.c_int), ('device', DeviceData * 1)]


class StreamControl(c.Structure):
    _fields_ = [('size', c.c_int), ('syncPinMode', c.c_int), ('auxPinMode', c.c_int),
                ('internalClockSource', c.c_int), ('internalClockFrequency', c.c_float),
                ('deviceFilterBitmask', c.c_uint)]


def decode_device(data, names, timestamp_hz=8000):
    count = data.channelData.count
    if count != len(names) or not 1 <= count <= 32:
        raise ValueError('SDK channel count changed; reconnect before collecting')
    channels = {name.upper(): float(data.channelData.data[i]) for i, name in enumerate(names)}
    if 'FZ' not in channels:
        raise ValueError('No documented FZ channel on this device; inspect reported channel names')
    if not all(math.isfinite(v) for v in channels.values()):
        raise ValueError('Nonfinite SDK channel data; recording invalidated')
    return {'t': data.additionalData.timestamp / float(timestamp_hz),
            'timestamp_ticks': int(data.additionalData.timestamp),
            'frame_counter': int(data.additionalData.frameCounter),
            'fz_n': channels['FZ'],
            # Only unambiguous anatomical labels. Never infer sides from channel order.
            'left_n': channels.get('FZL'), 'right_n': channels.get('FZR'),
            'channels': channels}


class NativeBridge:
    def __init__(self, dll_dir=None):
        self.dll_dir = Path(dll_dir or Path(__file__).parent / 'vendor' / 'x64')
        self.dll = None
        self.handle = None
        self.buffer = None
        self.buffer_size = c.c_size_t()
        self.devices = []
        self.streaming = False
        self.version = None
        self._dll_path_handle = None
        self.message = 'Disconnected'
        self.state = 'disconnected'

    def _bind(self, name, restype, *args):
        fn = getattr(self.dll, 'bertec_' + name)
        fn.restype = restype
        fn.argtypes = list(args)
        return fn

    def _check(self, result, name):
        if result != 0:
            raise RuntimeError(f'Bertec {name} failed: SDK status {result}')

    def open(self):
        if self.handle:
            self.close()
        if os.name != 'nt' or not (self.dll_dir / 'BertecDevice.dll').is_file():
            raise RuntimeError('Bertec x64 SDK unavailable; Windows and vendor/x64/BertecDevice.dll required')
        if not self.dll:
            try:
                self._dll_path_handle = os.add_dll_directory(str(self.dll_dir.resolve()))
                self.dll = c.CDLL(str((self.dll_dir / 'BertecDevice.dll').resolve()))
            except OSError as exc:
                raise RuntimeError(f'Bertec SDK could not load: {exc}') from exc
            P = c.c_void_p
            self._bind('LibraryVersion', c.c_uint)
            self._bind('Init', P)
            for name in ['Start', 'Stop', 'Close', 'GetStatus', 'GetDeviceCount', 'ZeroNow', 'ClearBufferedData']:
                self._bind(name, c.c_int, P)
            for name in ['ChangeMaxBufferedDataSize', 'SetEnableAutozero', 'SetAveraging', 'SetLowpassFiltering']:
                self._bind(name, c.c_int, P, c.c_int)
            self._bind('GetDeviceInfo', c.c_int, P, c.c_int, c.POINTER(DeviceInfo), c.c_size_t)
            for name in ['CanStartDataStream', 'StartDataStream']:
                self._bind(name, c.c_int, P, c.POINTER(StreamControl))
            self._bind('AllocateReadBufferedData', P, P, c.POINTER(c.c_size_t))
            self._bind('FreeAllocatedReadBufferedData', c.c_int, P, P)
            self._bind('ReadBufferedDataStream', c.c_int, P, P, c.c_size_t)
        version = self.dll.bertec_LibraryVersion()
        self.version = f'{version:x}'[0] + '.' + f'{version:x}'[1:]
        if version != 0x257:
            raise RuntimeError(f'Unsupported SDK ABI {version:x}; this bridge was verified for 2.57')
        self.handle = self.dll.bertec_Init()
        if not self.handle:
            raise RuntimeError('Bertec SDK init failed. Check FTDI/Bertec USB driver installation and x64 CPU support.')
        try:
            allocated = self.dll.bertec_ChangeMaxBufferedDataSize(self.handle, 5000)
            if allocated < 5000:
                raise RuntimeError(f'Bertec buffer allocation failed: {allocated}')
            self._check(self.dll.bertec_SetEnableAutozero(self.handle, 0), 'disable autozero')
            self._check(self.dll.bertec_SetAveraging(self.handle, 1), 'disable averaging')
            self._check(self.dll.bertec_SetLowpassFiltering(self.handle, 0), 'disable filter')
            self._check(self.dll.bertec_Start(self.handle), 'Start')
        except Exception:
            self.close()
            raise
        self.state, self.message = 'searching', 'SDK loaded. Searching for connected Bertec devices.'

    def _free_buffer(self):
        if self.buffer and self.handle:
            self.dll.bertec_FreeAllocatedReadBufferedData(self.handle, self.buffer)
        self.buffer = None
        self.streaming = False

    def _start_stream(self):
        count = self.dll.bertec_GetDeviceCount(self.handle)
        # This app targets the photographed single BP5046. Do not silently sum
        # several unsynchronized plates or choose a different person's platform.
        if count != 1:
            raise RuntimeError(f'Expected one Bertec device; detected {count}. Connect only the target plate.')
        info = DeviceInfo()
        self._check(self.dll.bertec_GetDeviceInfo(self.handle, 0, c.byref(info), c.sizeof(info)), 'GetDeviceInfo')
        if info.status != 0 or not 1 <= info.channelCount <= 32:
            raise RuntimeError(f'Device not healthy: status {info.status}, channels {info.channelCount}')
        names = [bytes(info.channelNames[i]).split(b'\0')[0].decode('ascii', 'replace') for i in range(info.channelCount)]
        if 'FZ' not in [n.upper() for n in names]:
            raise RuntimeError(f'FZ channel not found. Reported channels: {names}')
        self.devices = [{'index': 0, 'serial': info.serial.decode('ascii', 'replace'),
                         'model': info.model.decode('ascii', 'replace'), 'sample_rate': info.samplingFreq,
                         'channels': names, 'firmware': info.fwVersion,
                         # Legacy devices use an SDK-generated millisecond clock.
                         # Verified on AM6147-E: 4004 ticks over 4004 samples at 1kHz.
                         # Hardware timestamps use the SDK's documented 8kHz clock.
                         'has_hardware_timestamp': bool(info.has64BitTimestamp),
                         'timestamp_hz': 8000 if info.has64BitTimestamp else 1000}]
        control = StreamControl()
        control.size = c.sizeof(control)
        self._check(self.dll.bertec_CanStartDataStream(self.handle, c.byref(control)), 'CanStartDataStream')
        # SDK2.57 returns BERTEC_STREAM_SUCCESSFUL (-551) on this call.
        # Do not apply the generic zero-only convention to a stream result.
        stream_result = self.dll.bertec_StartDataStream(self.handle, c.byref(control))
        if stream_result not in (0, -551):
            self._check(stream_result, 'StartDataStream')
        self.buffer = self.dll.bertec_AllocateReadBufferedData(self.handle, c.byref(self.buffer_size))
        if not self.buffer or self.buffer_size.value < c.sizeof(Frame):
            raise RuntimeError('SDK frame allocation does not match pack-8 ABI')
        self.streaming = True
        self.state, self.message = 'waiting', 'Device detected; waiting for calibrated force samples.'

    def poll(self):
        if not self.handle:
            return []
        status = self.dll.bertec_GetStatus(self.handle)
        if status in (-5, -45, -46, -11, -12):
            self._free_buffer()
            self.devices = []
            self.state = 'searching' if status in (-45, -46) else 'waiting'
            self.message = {-5: 'No Bertec devices found. Connect USB, verify power and the Bertec USB driver.',
                            -45: 'Searching for Bertec devices.', -46: 'Initializing Bertec device.',
                            -11: 'Device stopped sending data. Check USB and power.',
                            -12: 'Bertec device fault. Check hardware before reconnecting.'}[status]
            return []
        if status == -4:
            raise RuntimeError('SDK buffer overflow: samples lost. Recording stopped; reconnect.')
        if not self.streaming and status == -50:
            self._start_stream()
        if not self.streaming:
            return []
        if status not in (0, -50, -51, -52, -551):
            raise RuntimeError(f'Unexpected SDK streaming status {status}')
        result = []
        for _ in range(5000):
            rc = self.dll.bertec_ReadBufferedDataStream(self.handle, self.buffer, self.buffer_size.value)
            if rc == 0:
                break
            if rc < 0:
                raise RuntimeError(f'SDK read failed: {rc}')
            frame = c.cast(self.buffer, c.POINTER(Frame)).contents
            if frame.deviceCount != 1:
                raise RuntimeError('Device count changed while streaming; reconnect')
            result.append(decode_device(frame.device[0], self.devices[0]['channels'],
                                        self.devices[0]['timestamp_hz']))
        if result:
            self.state, self.message = 'streaming', 'Live calibrated force data from Bertec USB.'
        return result

    def zero(self):
        if not self.streaming:
            raise RuntimeError('Connect a streaming plate before zeroing')
        self._check(self.dll.bertec_ZeroNow(self.handle), 'ZeroNow')
        self._check(self.dll.bertec_ClearBufferedData(self.handle), 'ClearBufferedData')

    def close(self):
        self._free_buffer()
        if self.handle:
            # Stop returns -34 during asynchronous startup. Closing the native
            # handle then races its worker and can cause a fatal access violation.
            deadline = time.monotonic() + 75
            while True:
                rc = self.dll.bertec_Stop(self.handle)
                if rc in (0, -32):
                    break
                if rc not in (-33, -34) or time.monotonic() >= deadline:
                    raise RuntimeError(f'Bertec Stop not completed ({rc}); handle retained safely')
                time.sleep(0.02)
            self._check(self.dll.bertec_Close(self.handle), 'Close')
        self.handle = None
        self.devices = []
        self.state, self.message = 'disconnected', 'Disconnected'

    def status(self):
        return {'state': self.state, 'message': self.message, 'sdk_version': self.version,
                'devices': list(self.devices)}
