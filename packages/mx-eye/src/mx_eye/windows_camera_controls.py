"""DirectShow controls, using the same explicit manual flags as the camera probe."""

class WindowsCameraControls:
    def __init__(self, index):
        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
        from ctypes import POINTER, c_long
        from pygrabber.dshow_graph import FilterGraph

        self.comtypes = comtypes
        comtypes.CoInitialize()

        def methods():
            return [
                COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "Property"),
                          *[(["out"], POINTER(c_long), n) for n in ("Minimum", "Maximum", "Step", "Default", "Caps")]),
                COMMETHOD([], HRESULT, "Set", (["in"], c_long, "Property"),
                          (["in"], c_long, "Value"), (["in"], c_long, "Flags")),
                COMMETHOD([], HRESULT, "Get", (["in"], c_long, "Property"),
                          (["out"], POINTER(c_long), "Value"), (["out"], POINTER(c_long), "Flags")),
            ]

        class CameraControl(IUnknown):
            _iid_ = GUID("{C6E13370-30AC-11D0-A18C-00A0C9118956}")
            _methods_ = methods()

        class VideoProcAmp(IUnknown):
            _iid_ = GUID("{C6E13360-30AC-11D0-A18C-00A0C9118956}")
            _methods_ = methods()

        self.graph = FilterGraph()
        self.entries = {}
        try:
            self.graph.add_video_input_device(index)
            instance = self.graph.get_input_device().instance
            for name, interface, prop in (("exposure", CameraControl, 4), ("gain", VideoProcAmp, 9)):
                try:
                    obj = instance.QueryInterface(interface)
                    lo, hi, step, default, caps = obj.GetRange(prop)
                    self.entries[name] = (obj, prop, dict(min=lo, max=hi, step=max(1, step),
                                                         default=default, caps=caps))
                except comtypes.COMError:
                    pass
        except Exception:
            self.close()
            raise

    def describe(self):
        return {name: dict(entry[2]) for name, entry in self.entries.items()}

    def set(self, name, value, auto=False):
        if name not in self.entries:
            raise RuntimeError(f"Camera does not advertise {name} control")
        obj, prop, limits = self.entries[name]
        flag = 1 if auto else 2
        if not limits["caps"] & flag:
            raise RuntimeError(f"Camera does not advertise requested {name} mode")
        value = limits['min'] + round((value - limits['min']) / limits['step']) * limits['step']
        value = max(limits['min'], min(limits['max'], value))
        obj.Set(prop, value, flag)
        readback, flags = obj.Get(prop)
        return f"{name} requested={value}, readback={readback}, mode={'auto' if flags & 1 else 'manual'}"

    def close(self):
        self.entries.clear()
        self.graph.remove_filters()
        self.graph = None
        self.comtypes.CoUninitialize()
