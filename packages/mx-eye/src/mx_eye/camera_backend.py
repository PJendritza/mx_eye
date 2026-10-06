"""Camera discovery/control without Qt: DirectShow on Windows, V4L2 on Linux.

Linux uses the system v4l2-ctl utility; no ctypes ABI guesses or elevated rights.
Only advertised capture modes/controls are offered. CSI/libcamera is a separate
backend and is not treated as a USB/UVC camera.
"""
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

import cv2


def v4l2(device, *args):
    if not shutil.which('v4l2-ctl'):
        raise RuntimeError('Install Linux camera tools: sudo apt install v4l-utils')
    result = subprocess.run(['v4l2-ctl', '--device', str(device), *args],
                            capture_output=True, text=True, timeout=4)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout


def parse_modes(text):
    """Parse discrete modes; do not invent intermediate stepwise sizes/rates."""
    modes, code, size = [], None, None
    for line in text.splitlines():
        match = re.search(r"\[\d+\]:\s+'(.{4})'", line)
        if match:
            code, size = match[1], None
        match = re.search(r'Size:\s+Discrete\s+(\d+)x(\d+)', line)
        if match:
            size = tuple(map(int, match.groups()))
        match = re.search(r'Interval:\s+Discrete.*\(([\d.]+)\s+fps\)', line)
        if match and code and size:
            modes.append(dict(width=size[0], height=size[1], fourcc=code, fps=float(match[1])))
    return modes


def parse_controls(text):
    controls = {}
    current = None
    for line in text.splitlines():
        match = re.match(r'\s*(\w+)\s+0x[0-9a-fA-F]+\s+\(([^)]+)\)\s*:\s*(.*)', line)
        if match:
            values = {key: int(value) for key, value in
                      re.findall(r'(min|max|step|default|value)=(-?\d+)', match[3])}
            controls[match[1]] = dict(values, type=match[2], flags=match[3])
            current = controls[match[1]]
        elif current is not None:
            menu = re.match(r'\s+(\d+):\s+(.+)', line)
            if menu:
                current.setdefault('choices', {})[int(menu[1])] = menu[2]
    return controls


def discover():
    cameras = []
    if sys.platform.startswith('linux'):
        if not shutil.which('v4l2-ctl'):
            raise RuntimeError('Install camera discovery on the Pi: sudo apt install v4l-utils')
        # Metadata-only nodes (often every other /dev/videoN) have no image modes.
        paths = sorted(Path('/dev').glob('video[0-9]*'), key=lambda p: int(p.name[5:]))
        for path in paths:
            index = int(path.name[5:])
            name_path = Path('/sys/class/video4linux') / path.name / 'name'
            name = name_path.read_text().strip() if name_path.exists() else path.name
            try:
                modes = parse_modes(v4l2(path, '--list-formats-ext'))
                if not modes:
                    continue
                try:
                    controls = parse_controls(v4l2(path, '--list-ctrls-menus'))
                except (RuntimeError, subprocess.TimeoutExpired):
                    controls = {}
                cameras.append(dict(index=index, name=name, device=str(path), backend='v4l2',
                                    formats=modes, controls=controls, error=''))
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                cameras.append(dict(index=index, name=name, device=str(path), backend='v4l2',
                                    formats=[], controls={}, error=str(exc)))
        return cameras
    if sys.platform != 'win32':
        raise RuntimeError('Automatic discovery supports Windows and Linux USB/UVC cameras.')
    try:
        from pygrabber.dshow_graph import FilterGraph
    except ImportError as exc:
        raise RuntimeError('Install camera discovery: python -m pip install pygrabber') from exc
    graph = FilterGraph()
    names = graph.get_input_devices()
    for index, name in enumerate(names):
        graph = FilterGraph()
        formats, error = [], ''
        try:
            graph.add_video_input_device(index)
            for mode in graph.get_input_device().get_formats():
                width, height = abs(int(mode['width'])), abs(int(mode['height']))
                if not (32 <= width <= 16384 and 32 <= height <= 16384):
                    continue
                rate = max(float(mode['min_framerate']), float(mode['max_framerate']))
                code = mode['media_type_str'].removeprefix('MEDIASUBTYPE_').upper()
                code = {'RGB24': 'BGR3', 'RGB32': 'BGR4', 'YUYV': 'YUY2'}.get(code, code)
                formats.append(dict(width=width, height=height, fps=rate,
                                    min_fps=min(float(mode['min_framerate']), float(mode['max_framerate'])), fourcc=code))
        except Exception as exc:
            error = str(exc)
        finally:
            graph.remove_filters()
        cameras.append(dict(index=index, name=name, device='', backend='dshow',
                            formats=formats, controls={}, error=error))
    return cameras


def backend_for(source):
    backend = source['backend']
    if sys.platform.startswith('linux') and backend in ('dshow', 'msmf'):
        backend = 'v4l2'
    elif sys.platform == 'win32' and backend == 'v4l2':
        backend = 'dshow'
    if backend == 'auto':
        backend = 'dshow' if sys.platform == 'win32' else 'v4l2' if sys.platform.startswith('linux') else 'auto'
    return {'auto': cv2.CAP_ANY, 'dshow': cv2.CAP_DSHOW,
            'msmf': cv2.CAP_MSMF, 'v4l2': cv2.CAP_V4L2}[backend]


def camera_target(source):
    if backend_for(source) == cv2.CAP_V4L2:
        return source.get('device') or f"/dev/video{source['camera']}"
    return int(source['camera'])


def apply_controls(cap, source):
    """Explicit requests and readbacks; never claim a successful set proves exposure."""
    exposure = source.get('exposure_mode', 'unchanged')
    gain = source.get('gain')
    replies = []
    if backend_for(source) == cv2.CAP_V4L2:
        if exposure == 'unchanged' and gain is None:
            return 'Camera exposure/gain unchanged.'
        device = camera_target(source)
        controls = parse_controls(v4l2(device, '--list-ctrls-menus'))
        # Names changed in v4l-utils; select names actually returned by the driver.
        auto = next((n for n in ('auto_exposure', 'exposure_auto') if n in controls), None)
        absolute = next((n for n in ('exposure_time_absolute', 'exposure_absolute') if n in controls), None)
        requests = []
        if exposure != 'unchanged':
            if auto:
                choices = controls[auto].get('choices', {})
                value = 1 if exposure == 'manual' else (3 if 3 in choices else 0)
                if choices and value not in choices:
                    replies.append('Requested exposure mode not advertised')
                else:
                    requests.append((auto, value))
            else:
                replies.append('Exposure mode control not advertised')
            if exposure == 'manual':
                if absolute:
                    requests.append((absolute, round(source['exposure_ms'] * 10)))
                else:
                    replies.append('Absolute exposure control not advertised')
                priority = next((n for n in ('exposure_dynamic_framerate', 'exposure_auto_priority') if n in controls), None)
                if priority:
                    requests.append((priority, 0))
        if gain is not None:
            if 'gain' in controls:
                if 'autogain' in controls:
                    requests.append(('autogain', 0))
                if 'auto_gain' in controls:
                    requests.append(('auto_gain', 0))
                requests.append(('gain', round(gain)))
            else:
                replies.append('Manual gain not advertised')
        for name, value in requests:
            limits = controls[name]
            if limits['type'] not in ('menu','intmenu') and 'min' in limits and 'max' in limits:
                step = max(1, limits.get('step', 1))
                value = limits['min'] + round((value - limits['min']) / step) * step
                value = max(limits['min'], min(limits['max'], value))
            try:
                v4l2(device, f'--set-ctrl={name}={value}')
                replies.append(v4l2(device, f'--get-ctrl={name}').strip())
            except RuntimeError as exc:
                replies.append(f'{name}: {exc}')
    else:
        if exposure != 'unchanged':
            ok = cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, .25 if exposure == 'manual' else .75)
            replies.append(f'auto mode request={ok}')
            if exposure == 'manual':
                raw = round(math.log2(source['exposure_ms'] / 1000))
                ok = cap.set(cv2.CAP_PROP_EXPOSURE, raw)
                replies.append(f'exposure set={ok}, readback={cap.get(cv2.CAP_PROP_EXPOSURE):g} (log2 seconds)')
        if gain is not None:
            ok = cap.set(cv2.CAP_PROP_GAIN, gain)
            replies.append(f'gain set={ok}, readback={cap.get(cv2.CAP_PROP_GAIN):g}')
    return '; '.join(replies) or 'Camera exposure/gain unchanged.'


def validated_camera_source(source, cameras):
    """Keep a supported selection; repair stale requests using advertised modes.

    Advertised maxima constrain requests, never prove delivered frame rate.
    Discovery may be unavailable; manual requests then remain possible.
    """
    from .config import SourceConfig
    camera = next((c for c in cameras if c['index'] == source.camera), None)
    names = [c for c in cameras if source.camera_name and c['name'] == source.camera_name]
    if len(names) == 1:
        camera = names[0]
    if camera is None:
        return source.model_copy(deep=True)
    values = source.model_dump(mode='json')
    values.update(camera=camera['index'], camera_name=camera['name'], device=camera.get('device', ''))
    modes = [m for m in camera['formats'] if len(m['fourcc']) == 4 and math.isfinite(m['fps']) and m['fps'] >= 1]
    if modes:
        matching = [m for m in modes if (m['width'], m['height']) == (source.width, source.height)]
        if not matching:
            fast = [m for m in modes if m['fps'] >= 29]
            selected = max(fast or modes, key=lambda m: (m['width'] * m['height'], m['fps'], m['fourcc'] == 'MJPG')
                           if fast else (m['fps'], m['width'] * m['height'], m['fourcc'] == 'MJPG'))
            matching = [m for m in modes if (m['width'], m['height']) == (selected['width'], selected['height'])]
        same_format = [m for m in matching if m['fourcc'] == source.fourcc]
        selected = max(same_format or matching, key=lambda m: (m['fps'], m['fourcc'] == 'MJPG'))
        rates = [m['fps'] for m in matching if m['fourcc'] == selected['fourcc']]
        requested = min(source.fps, max(rates))
        # V4L2 discovery lists discrete intervals; Windows reports a range.
        if camera.get('backend') == 'v4l2':
            requested = min(rates, key=lambda rate: abs(rate - requested))
        else:
            candidates = [min(m['fps'], max(max(1, m.get('min_fps', 1)), source.fps))
                          for m in matching if m['fourcc'] == selected['fourcc']]
            requested = min(candidates, key=lambda rate: abs(rate - source.fps))
        values.update(width=selected['width'], height=selected['height'], fourcc=selected['fourcc'], fps=requested)
    return SourceConfig(**values)
