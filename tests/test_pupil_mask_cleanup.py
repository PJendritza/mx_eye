import cv2
import numpy as np
import pytest

from mx_eye.config import MxEyeConfigModel, PupilMethod, TrackingConfig, TrackingMode
from mx_eye.tracking import Tracker, clean_mask

PUPIL = (100.0, 110.0)


def eye(bridge_width=5):
    """Pupil joined to a dark eyelid band above it by a narrow dark bridge."""
    gray = np.full((200, 200), 160, np.uint8)
    cv2.circle(gray, (100, 110), 22, 20, -1)
    gray[60:68, 60:141] = 30  # dark upper arc / eyelid shadow
    half = bridge_width // 2
    gray[66:90, 100 - half:100 + half + 1] = 30
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def pupil_center(cleanup, method=PupilMethod.THRESHOLD, frame=None):
    config = TrackingConfig(
        roi=(0, 0, 200, 200),
        tracking_mode=TrackingMode.PUPIL_ONLY,
        pupil_method=method,
        pupil_thr=60,
        pupil_mask_cleanup=cleanup,
    )
    result = Tracker(config).process(eye() if frame is None else frame, 1)
    assert result["valid"], result["reject_reason"]
    return result["pupil"]["x"], result["pupil"]["y"]


def test_merged_mask_biases_center_without_cleanup():
    x, y = pupil_center(0)
    assert abs(x - PUPIL[0]) < 1
    assert y < PUPIL[1] - 5  # pulled towards the arc


def test_cleanup_separates_pupil_from_arc():
    x, y = pupil_center(3)
    assert abs(x - PUPIL[0]) < 0.5 and abs(y - PUPIL[1]) < 0.5


def test_cleanup_keeps_isolated_pupil_center():
    frame = np.full((200, 200, 3), 160, np.uint8)
    cv2.circle(frame, (100, 110), 22, (20, 20, 20), -1)
    for radius in (0, 3, 6):
        x, y = pupil_center(radius, frame=frame)
        assert abs(x - PUPIL[0]) < 0.5 and abs(y - PUPIL[1]) < 0.5


def test_clean_mask_zero_is_identity_and_removes_thin_lines():
    mask = np.zeros((50, 50), np.uint8)
    mask[10:40, 25:28] = 255  # 3 px wide line
    assert clean_mask(mask, 0) is mask
    assert not clean_mask(mask, 2).any()


def test_setting_is_validated_and_defaults_off():
    assert TrackingConfig().pupil_mask_cleanup == 0
    with pytest.raises(ValueError):
        TrackingConfig(pupil_mask_cleanup=11)
    assert MxEyeConfigModel().tracking.pupil_mask_cleanup == 0
