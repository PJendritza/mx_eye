"""Advertised camera modes constrain requests, not measured delivery rates."""
from mx_eye.camera_backend import validated_camera_source
from mx_eye.config import SourceConfig


def cameras(backend='dshow'):
    return [dict(index=0, name='C920', device='', backend=backend, formats=[
        dict(width=640, height=480, fourcc='MJPG', fps=30, min_fps=5),
        dict(width=1280, height=720, fourcc='MJPG', fps=30, min_fps=5),
        dict(width=1280, height=720, fourcc='YUY2', fps=10, min_fps=5),
    ])]


def test_stale_fps_is_clamped_without_changing_supported_resolution():
    result = validated_camera_source(SourceConfig(fps=120), cameras())
    assert (result.width, result.height, result.fourcc, result.fps) == (640, 480, 'MJPG', 30)
    assert result.camera_name == 'C920'


def test_supported_settings_survive_discovery():
    source = SourceConfig(width=1280, height=720, fourcc='YUY2', fps=7)
    result = validated_camera_source(source, cameras())
    assert (result.width, result.height, result.fourcc, result.fps) == (1280, 720, 'YUY2', 7)


def test_missing_mode_selects_advertised_resolution_and_format():
    result = validated_camera_source(SourceConfig(width=999, height=777, fourcc='ABCD'), cameras())
    assert (result.width, result.height, result.fourcc, result.fps) == (1280, 720, 'MJPG', 30)


def test_failed_discovery_keeps_manual_request():
    source = SourceConfig(fps=120)
    assert validated_camera_source(source, []) == source


def test_linux_uses_advertised_discrete_interval():
    result = validated_camera_source(SourceConfig(fps=17), cameras('v4l2'))
    assert result.fps == 30


def test_windows_separate_advertised_fps_ranges():
    found = cameras()
    found[0]['formats'][0]['min_fps'] = 30
    found[0]['formats'].append(dict(width=640, height=480, fourcc='MJPG', fps=15, min_fps=15))
    assert validated_camera_source(SourceConfig(fps=15), found).fps == 15
