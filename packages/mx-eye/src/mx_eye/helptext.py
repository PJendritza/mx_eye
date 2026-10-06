"""Short explanations shared by the tracker controls."""
from PySide6 import QtWidgets as W

TIPS={
 'pupil_thr':'Original threshold method only: pixels darker than this intensity are pupil candidates (0–255). Raising it may include iris or eyelashes.',
 'pupil_rays':'Starburst-style: number of outward rays used to find dark-to-light pupil edges. More rays supply more points but require more work.',
 'pupil_edge_contrast':'Minimum outward brightness increase (0–255 units). Starburst checks across two radial pixels; both edge methods also require a darker interior than exterior. Lower values admit weaker edges and more noise.',
 'pupil_edge_threshold':'Edge + ellipse: lower Canny gradient threshold; the upper threshold is twice this value. Raise it to suppress weak/noisy edges.',
 'pupil_fit_error':'Approximate maximum ellipse-fitting residual in source pixels. Smaller values reject more edge points; larger values tolerate noise and imperfect boundaries.',
 'pupil_adaptive_window':'Adaptive threshold: Gaussian neighborhood width in source pixels (rounded up to an odd size). Start larger than the pupil diameter so its dark interior remains included.',
 'pupil_adaptive_offset':'Adaptive threshold: subtract this intensity from the local mean before selecting dark pixels. Higher values require stronger local darkness.',
 'pupil_min':'Minimum pupil area in source pixels²: dark connected-region area for threshold methods; fitted ellipse area for edge methods.',
 'pupil_max':'Maximum pupil area in source pixels²: dark connected-region area for threshold methods; fitted ellipse area for edge methods.',
 'cr_thr':'Pixels brighter than this intensity are reflection candidates (0–255). Raising it keeps only brighter reflections.',
 'cr_min':'Smallest bright region accepted as a corneal reflection, in source-image pixels².',
 'cr_max':'Largest bright region accepted as a corneal reflection, in source-image pixels². Helps reject broad glare.',
 'pupil_gate':'Maximum pupil distance from its predicted position, in source pixels. Too small can reject fast eye movements; too large admits unrelated regions.',
 'cr_gate':'Maximum reflection distance from its predicted position, in source pixels. Smaller values reduce switches between reflections.',
 'max_pair_dist':'Largest allowed distance between pupil and CR centers, in source pixels. Used in Pupil + CR mode.',
 'max_pair_vec_change':'Largest allowed change of the pupil–CR vector, in pixels. Too small can reject real saccades.',
 'reacquire_after_frames':'After this many failed frames, relax continuity constraints to look for the pupil/CR again.',
 'template_radius':'Radius of the captured template, in source pixels. The magenta circle previews this size; right-click to capture a new template.',
 'template_search_size':'Side length of the template search region, in source pixels. Larger regions tolerate more motion but require more processing.',
 'template_min_corr':'Minimum normalized template correlation (0–1) required to move the ROI. Higher values demand a closer appearance match.',
 'bind':'Local address on which the tracker listens. 127.0.0.1 limits connections to this computer; 0.0.0.0 listens on all interfaces.',
 'data_port':'Port carrying eye-position samples to the SDK.',
 'control_port':'Port accepting SDK status, start and stop requests.',
 'sync_port':'Port used to estimate the clock offset for latency measurements between computers.',
 'directory':'Folder in which each recording session is saved.',
 'buffer_mb':'Memory reserved for queued video frames, in MiB. A larger buffer tolerates longer disk/encoding stalls.',
 'codec':'MJPG records compressed AVI; FFV1 records lossless MKV and can require more processing.',
 'record_simulation':'Also save full video when using the artificial-eye simulation.',
 'camera_mjpeg_passthrough':'On Linux with FFmpeg and a compatible MJPEG camera, save original JPEG packets without re-encoding. Otherwise use normal full-resolution recording. FFV1 always uses decoded frames.',
 'hz':'Maximum live preview and plot update rate. Lower values leave more CPU and memory bandwidth for tracking and recording. File navigation still displays each processed frame.',
}

BUTTONS={
 'Open video…':'Choose a video file to open and begin playback immediately. An active session is stopped first.',
 'Start':'Start acquisition and tracking. Live camera mode also records the full video.',
 'Stop':'Stop tracking and finish saving queued recording data.',
 'Settings':'Configure camera, network and recording options while the session is stopped.',
 'Camera settings…':'Detect cameras, refresh the list, and select resolution and requested frame rate.',
 'Load config…':'Import tracking, display, source and network settings from a JSON file. Stop the current session first.',
 'Save config…':'Save current settings and the captured template in a JSON file.',
 'Load template…':'Load a saved eye-template image for locating the ROI.',
 'Save template…':'Export the current eye template as a PNG image.',
 'Clear template':'Discard the captured template and stop template-based ROI following.',
 'Reset tracking history':'Clear pupil/CR detection history so tracking can acquire them again.',
 'Choose output folder…':'Choose where new recording sessions will be saved.',
 'Refresh':'Scan for connected cameras again, for example after plugging in a camera.',
 'Save':'Apply these settings and close the dialog.',
 'Cancel':'Close without applying changes.',
}

def add_tooltips(window):
    explanations={
      'source':'Choose live camera, a video file, or an artificial eye. Open video stops an active session and starts the selected file.',
      'requested_fps':'Requested camera frame rate; change it in Camera settings. ACQ shows the measured frame rate.',
      'mode':'Pupil + CR outputs pupil minus reflection coordinates. Pupil only outputs pupil position in the selected coordinate system.',
      'pupil_method':'Choose the pupil detector. Threshold is the original method; Starburst-style uses radial edges; Edge + ellipse uses Canny contours; Adaptive threshold uses local brightness. Switching clears tracking history. Template and CR tracking stay active.',
      'pupil_mask':'Show or hide pupil evidence and the fitted outline: dark pixels for threshold methods, Canny edges for Edge + ellipse, accepted ray points for Starburst-style. Display only.',
      'cr_mask':'Show or hide red pixels that pass the reflection threshold. Display only; detection is unchanged.',
      'crosshairs':'Show or hide detected pupil and reflection centers.',
      'circle':'Show or hide the magenta circle indicating the template size.',
      'inset':'Show or hide the captured template inset inside the source panel.',
      'template_on':'Move the yellow eye ROI with the matched template when correlation exceeds the minimum.',
      'pause':'Play or pause file playback. When paused, tracking settings can still be adjusted.',
      'step':'Pause and advance one video frame. Keyboard: Right arrow.',
      'back':'Pause and go back one video frame. Keyboard: Left arrow.',
      'timeline':'Click to seek and pause on that frame, or drag to choose a frame. Left/Right arrows step one frame. Large jumps may take time to decode.',
      'speed':'Video playback multiplier. 1× is recorded speed; 0.5× is half speed. Does not change camera FPS.',
      'trace':'Recent X/Y output in pixels. The title identifies the active coordinate system.',
      'xy':'Recent X/Y trajectory in pixels, with the latest valid position marked. Y increases downward.',
      'metrics':'ACQ: frames acquired per second. TRACK: frames processed per second. PROC: tracking computation time. SKIPPED: frames not tracked. VIDEO: written frames and recording queue.',
      'track_label':'Current pupil/CR detection status. Reacquisition means the tracker is looking for valid candidates again.',
      'show_reason':'Show or hide orange rejection reasons at the right of the performance bar. Errors remain visible in red. Display only.',
      'reason_label':'Orange: why tracking could not produce a valid position. Red: acquisition, tracking, recording or network error. Hover for full text.',
      'template_label':'Current template match status and correlation information.',
      'frame_label':'Displayed video frame number (starting at 1). For live sources, the acquisition frame counter.',
      'status':'Session state, errors, and the current recording folder.',
    }
    for name,text in explanations.items():
        widget=getattr(window,name,None)
        if isinstance(widget,W.QWidget): widget.setToolTip(text)
    for button in window.findChildren(W.QAbstractButton):
        text=button.text().replace('&','')
        if text in BUTTONS: button.setToolTip(BUTTONS[text])
    camera=getattr(window,'camera_controls',None)
    if camera:
        for name,text in {
            'camera':'Connected camera names and device indices: DirectShow on Windows, V4L2 on Linux. Select a camera to list its reported resolutions.',
            'resolution':'Driver-reported sizes. Defaults to the largest size with at least 29 fps, or the fastest mode if none qualifies.',
            'fps':'Requested frame rate for the selected resolution. The driver may deliver a lower rate.',
            'index':'Manual camera index used when automatic discovery is unavailable.',
            'width':'Requested image width in source pixels.',
            'height':'Requested image height in source pixels.',
            'backend':'Camera driver interface. Auto selects DirectShow on Windows and V4L2 on Linux.',
            'fourcc':'Four-character camera pixel format, for example MJPG or YUY2. Must be supported by the selected camera.',
        }.items(): getattr(camera,name).setToolTip(text)
