# Motion-triggered camera monitor prototype

This is Patrick's standalone early test for recording camera footage when motion is detected. It is separate from `mx-eye` acquisition, tracking, and recording.

- Runs on Linux/Raspberry Pi with a V4L2 MJPEG camera or a Raspberry Pi CSI camera exposed by `rpicam-vid` (or `libcamera-vid`).
- Uses PyQt5, NumPy, OpenCV, FFmpeg, and camera discovery via `v4l2-ctl` for USB devices. These are **not** installed by the main `mx_eye` environment.
- Launch with `python3 prototypes/mxbi_motion_triggered_camera_monitor.py` after installing those dependencies.
- Motion triggering starts disarmed. Start the camera, select the motion area/threshold, then press **ARM**. Manual recording is available without arming.
- Writes settings and logs under `~/.mxbi_camera_monitor/`, temporary segments under `/dev/shm/mxbi_camera_monitor/`, and final MKV files under `~/mxbi_recordings/` by default.

This script is kept as a reference prototype. It has not been integrated with the tracker or verified here with a physical camera.
