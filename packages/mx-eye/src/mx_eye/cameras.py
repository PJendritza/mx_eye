"""Camera settings with isolated Windows DirectShow / Linux V4L2 discovery."""
import multiprocessing as mp
import queue
import sys
import time
from PySide6 import QtCore as C, QtWidgets as W
from .config import SourceConfig


def discover_cameras(output):
    try:
        from .camera_backend import discover
        output.put((discover(), ''))
    except Exception as exc:
        output.put(([], str(exc)))


class CameraModeDiscovery(C.QObject):
    """One asynchronous, session-local mode cache shared by main GUI and Settings."""
    finished = C.Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cameras = None
        self.error = ''
        self.process = self.output = None
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.poll)

    def ensure_loaded(self, force=False):
        if self.process is not None or (self.cameras is not None and not force):
            return
        context = mp.get_context('spawn')
        self.output = context.Queue()
        self.process = context.Process(target=discover_cameras, args=(self.output,), daemon=True)
        try:
            self.process.start()
        except (OSError, RuntimeError) as exc:
            self.complete([], str(exc))
            return
        self.started = time.monotonic()
        self.timer.start(100)

    def poll(self):
        try:
            cameras, error = self.output.get_nowait()
        except queue.Empty:
            if time.monotonic() - self.started < 20 and self.process.is_alive():
                return
            cameras, error = [], 'Camera discovery did not finish. Close other camera apps and Refresh, or use manual settings.'
        self.complete(cameras, error)

    def complete(self, cameras, error=''):
        self.stop()
        self.cameras, self.error = cameras, error
        self.finished.emit(cameras, error)

    def stop(self):
        self.timer.stop()
        if self.process is not None:
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.process.terminate()
                self.process.join(timeout=.5)
            self.process = None
        if self.output is not None:
            self.output.close()
            self.output = None


class CameraControls(W.QWidget):
    def __init__(self,source,parent=None,discovery=None):
        super().__init__(parent)
        self.source=source.model_dump(mode="json")
        source=self.source
        self.cameras=[]
        self.owns_discovery = discovery is None
        self.discovery = discovery or CameraModeDiscovery(self)
        self.discovery.finished.connect(self.load_modes)
        form=W.QFormLayout(self)
        form.setContentsMargins(0,0,0,0)
        row=W.QHBoxLayout()
        self.camera=W.QComboBox()
        self.camera.addItem(f"Camera {source['camera']} (saved index)",source['camera'])
        self.refresh=W.QPushButton('Refresh')
        row.addWidget(self.camera,1)
        row.addWidget(self.refresh)
        form.addRow('Detected camera',row)
        self.resolution=W.QComboBox()
        self.resolution.addItem(f"{source['width']} × {source['height']} (saved)",(source['width'],source['height']))
        form.addRow('Resolution',self.resolution)
        self.format=W.QComboBox()
        form.addRow('Camera format',self.format)
        self.info=W.QLabel('Scanning connected cameras…')
        self.info.setWordWrap(True)
        form.addRow(self.info)
        self.fps=W.QDoubleSpinBox()
        self.fps.setRange(1,1000)
        self.fps.setValue(source['fps'])
        form.addRow('Requested FPS',self.fps)
        self.manual=W.QGroupBox('Manual camera settings')
        manual=W.QFormLayout(self.manual)
        self.index=W.QSpinBox()
        self.index.setRange(0,99)
        self.index.setValue(source['camera'])
        self.width=W.QSpinBox()
        self.height=W.QSpinBox()
        for widget,key in [(self.width,'width'),(self.height,'height')]:
            widget.setRange(32,16384)
            widget.setValue(source[key])
        self.backend=W.QComboBox()
        self.backend.addItems(['auto','dshow','msmf'] if sys.platform=='win32' else ['auto','v4l2'])
        self.backend.setCurrentText(source['backend'])
        self.fourcc=W.QLineEdit(source['fourcc'])
        for text,widget in [('Index',self.index),('Width',self.width),('Height',self.height),('Backend',self.backend),('Format',self.fourcc)]:
            manual.addRow(text,widget)
        form.addRow(self.manual)
        self.exposure_mode=W.QComboBox()
        for title, value in [('Keep camera setting','unchanged'),('Manual exposure','manual'),('Automatic exposure','auto')]:
            self.exposure_mode.addItem(title,value)
        self.exposure_mode.setCurrentIndex(max(0,self.exposure_mode.findData(source.get('exposure_mode','unchanged'))))
        self.exposure=W.QDoubleSpinBox()
        self.exposure.setRange(.01,1000)
        self.exposure.setDecimals(2)
        self.exposure.setSuffix(' ms')
        self.exposure.setValue(source.get('exposure_ms',7.8125))
        self.exposure.setEnabled(self.exposure_mode.currentData()=='manual')
        self.exposure_mode.currentIndexChanged.connect(lambda: self.exposure.setEnabled(self.exposure_mode.currentData()=='manual'))
        self.exposure.setToolTip('Requested duration. Windows uses discrete log2(seconds) steps; Linux uses the camera’s V4L2 range and steps. Readbacks appear after Start.')
        self.exposure_mode.setToolTip('Keep the camera’s current exposure behavior, request automatic exposure, or set a fixed exposure duration when the session starts.')
        self.gain_on=W.QCheckBox('Set manual gain')
        self.gain_on.setChecked(source.get('gain') is not None)
        self.gain=W.QSpinBox()
        self.gain.setRange(0,65535)
        self.gain.setValue(source.get('gain') or 0)
        self.gain.setEnabled(self.gain_on.isChecked())
        self.gain_on.toggled.connect(self.gain.setEnabled)
        self.gain_on.setToolTip('Request manual gain at Start. Uncheck to leave gain unchanged in the driver.')
        self.gain.setToolTip('Raw driver gain units. Linux uses the advertised range. Unsupported requests are reported after Start.')
        form.addRow('Exposure mode',self.exposure_mode)
        form.addRow('Exposure duration',self.exposure)
        gain_row=W.QHBoxLayout()
        gain_row.addWidget(self.gain_on)
        gain_row.addWidget(self.gain)
        form.addRow('Gain',gain_row)

        self.camera.currentIndexChanged.connect(self.camera_changed)
        self.resolution.currentIndexChanged.connect(self.resolution_changed)
        self.format.currentIndexChanged.connect(self.format_changed)
        self.refresh.clicked.connect(self.scan)
        if self.discovery.cameras is not None:
            self.load_modes(self.discovery.cameras, self.discovery.error)
        elif parent is None or not getattr(parent.parent(), 'service', None) or not parent.parent().service.run:
            C.QTimer.singleShot(0, self.discovery.ensure_loaded)
        else:
            self.info.setText('Stop tracking to discover camera modes.')

    @property
    def process(self):
        return self.discovery.process

    def scan(self):
        self.info.setText('Scanning connected cameras…')
        self.discovery.ensure_loaded(force=True)

    def load_modes(self, cameras, error):
        if self.cameras:
            self.source = self.values().model_dump(mode="json")
        selected=self.camera.currentData()
        previous=next((item for item in self.cameras if item['index']==selected),None)
        selected_name=previous['name'] if previous else self.source.get('camera_name','')
        self.refresh.setEnabled(True)
        self.camera.setEnabled(True)
        self.resolution.setEnabled(True)
        self.format.setEnabled(True)
        self.cameras=cameras
        if error or not cameras:
            self.info.setText(error or 'No cameras detected. Plug in a camera and click Refresh.')
            with C.QSignalBlocker(self.camera):
                self.camera.clear()
            self.resolution.clear()
            self.format.clear()
            self.manual.setVisible(True)
            return
        with C.QSignalBlocker(self.camera):
            self.camera.clear()
            for camera in cameras:
                self.camera.addItem(f"{camera['name']} · {camera['index']}",camera['index'])
            matches=[i for i,item in enumerate(cameras) if item['name']==selected_name]
            self.camera.setCurrentIndex(matches[0] if len(matches)==1 else max(0,self.camera.findData(selected)))
        from .camera_backend import validated_camera_source
        validated = validated_camera_source(SourceConfig(**self.source), cameras)
        self.source = validated.model_dump(mode='json')
        with C.QSignalBlocker(self.camera):
            self.camera.setCurrentIndex(max(0, self.camera.findData(validated.camera)))
        self.camera_changed(preferred=(validated.width, validated.height))

    def camera_changed(self,*args,preferred=None):
        camera=next((item for item in self.cameras if item['index']==self.camera.currentData()),None)
        if camera is None: return
        self.index.setValue(camera['index'])
        gain_limits=camera.get('controls',{}).get('gain',{})
        self.gain.setRange(gain_limits.get('min',0),gain_limits.get('max',65535))
        self.gain.setSingleStep(max(1,gain_limits.get('step',1)))
        controls=camera.get('controls',{})
        exposure_limits=next((controls[n] for n in ('exposure_time_absolute','exposure_absolute') if n in controls),{})
        self.exposure.setRange(max(.01,exposure_limits.get('min',1)/10),min(1000,exposure_limits.get('max',10000)/10))
        self.exposure.setSingleStep(max(.01,exposure_limits.get('step',10)/10))
        self.backend.setCurrentText(self.source['backend'] if preferred is not None else camera.get('backend', 'auto'))
        usable=[m for m in camera['formats'] if len(m['fourcc'])==4]
        modes=usable or camera['formats']
        sizes=sorted({(m['width'],m['height']) for m in modes},key=lambda s:(s[0]*s[1],s[0]),reverse=True)
        fast_sizes={s for s in sizes if any(m['fps']>=29 and (m['width'],m['height'])==s for m in modes)}
        default_size=next((s for s in sizes if s in fast_sizes),sizes[0] if sizes else None)
        if not fast_sizes and modes:
            fastest=max(m['fps'] for m in modes)
            default_size=next(s for s in sizes if any(m['fps']==fastest and (m['width'],m['height'])==s for m in modes))
        with C.QSignalBlocker(self.resolution):
            self.resolution.clear()
            for w,h in sizes: self.resolution.addItem(f'{w} × {h}',(w,h))
            if preferred is not None and tuple(preferred) in sizes:
                self.resolution.setCurrentIndex(sizes.index(tuple(preferred)))
            elif default_size is not None:
                self.resolution.setCurrentIndex(sizes.index(default_size))
        self.manual.setVisible(not sizes)
        self.info.setText('Highest resolution reporting at least 29 fps selected.' if fast_sizes else
                          ('No mode reports 29 fps; fastest reported mode selected.' if sizes else
                           'This camera did not report its modes. Use manual settings. '+camera['error']))
        self.resolution_changed(preserve=preferred is not None)

    def resolution_changed(self, *args, preserve=False):
        size=self.resolution.currentData()
        if not size: return
        self.width.setValue(size[0])
        self.height.setValue(size[1])
        camera=next((item for item in self.cameras if item['index']==self.camera.currentData()),None)
        with C.QSignalBlocker(self.format):
            self.format.clear()
        if camera:
            modes=[m for m in camera['formats'] if (m['width'],m['height'])==tuple(size)]
            usable=[m for m in modes if len(m['fourcc'])==4]
            by_format={}
            for mode in usable:
                code=mode['fourcc']
                if code not in by_format or mode['fps']>by_format[code]['fps']:
                    by_format[code]=dict(mode)
            for code, mode in by_format.items():
                mode['min_fps'] = min(max(1, m.get('min_fps', 1)) for m in usable if m['fourcc'] == code)
            options=sorted(by_format.values(),key=lambda m:(m['fps'],m['fourcc']=='MJPG'),reverse=True)
            with C.QSignalBlocker(self.format):
                for mode in options:
                    self.format.addItem(f"{mode['fourcc']} · up to {mode['fps']:g} fps",mode)
            self.format.setEnabled(bool(options))
            self.manual.setVisible(not options)
            if preserve:
                index = next((i for i, mode in enumerate(options) if mode['fourcc'] == self.source['fourcc']), 0)
                with C.QSignalBlocker(self.format):
                    self.format.setCurrentIndex(index)
            self.format_changed()
            if preserve:
                self.fps.setValue(self.source['fps'])

    def format_changed(self):
        mode=self.format.currentData()
        if not mode: return
        self.fourcc.setText(mode['fourcc'])
        self.fps.setRange(max(1, mode.get('min_fps', 1)), max(1, mode['fps']))
        self.fps.setValue(max(1,mode['fps']))
        self.info.setText(f"{mode['width']} × {mode['height']} · {mode['fourcc']} · driver reports up to {mode['fps']:.1f} fps. Check measured ACQ after starting.")

    def values(self):
        camera=next((item for item in self.cameras if item['index']==self.index.value()),None)
        source = SourceConfig(**dict(self.source, camera=self.index.value(),width=self.width.value(),height=self.height.value(),
                    camera_name=camera['name'] if camera else self.source.get('camera_name',''),
                    device=camera.get('device','') if camera else
                           (self.source.get('device','') if self.index.value() == self.source['camera'] else ''),
                    exposure_mode=self.exposure_mode.currentData(),exposure_ms=self.exposure.value(),
                    gain=self.gain.value() if self.gain_on.isChecked() else None,
                    fps=self.fps.value(),backend=self.backend.currentText(),fourcc=self.fourcc.text().strip()))
        from .camera_backend import validated_camera_source
        return validated_camera_source(source, self.cameras)

    def stop_scan(self):
        if self.owns_discovery:
            self.discovery.stop()
        try:
            self.discovery.finished.disconnect(self.load_modes)
        except (RuntimeError, TypeError):
            pass
