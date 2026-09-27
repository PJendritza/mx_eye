"""Exact decoded-frame navigation with a bounded cache for nearby backward steps."""

from collections import OrderedDict

import cv2


class VideoReader:
    def __init__(self, path, stop, limit=64 * 1024 * 1024):
        self.path, self.stop, self.limit = path, stop, limit
        self.cap = cv2.VideoCapture(path)
        self.next_index = 0
        self.total = None
        self.cache = OrderedDict()
        self.bytes = 0

    def read(self, target):
        target = max(0, int(target))
        if self.total is not None:
            target = min(target, max(0, self.total - 1))
        if target in self.cache:
            frame, media = self.cache[target]
            return frame, target, media
        if target < self.next_index:
            self.cap.release()
            self.cap = cv2.VideoCapture(self.path)
            self.next_index = 0
        latest = None
        while not self.stop.is_set() and self.next_index <= target:
            ok, frame = self.cap.read()
            if not ok:
                self.total = self.next_index
                if latest is not None:
                    return latest
                if self.total - 1 in self.cache:
                    frame, media = self.cache[self.total - 1]
                    return frame, self.total - 1, media
                return None
            index = self.next_index
            self.next_index += 1
            media = self.cap.get(cv2.CAP_PROP_POS_MSEC)
            latest = (frame, index, media)
            if index in self.cache:
                self.bytes -= self.cache.pop(index)[0].nbytes
            self.cache[index] = (frame, media)
            self.bytes += frame.nbytes
            while self.bytes > self.limit and len(self.cache) > 1:
                _, (old, _) = self.cache.popitem(last=False)
                self.bytes -= old.nbytes
        return latest if not self.stop.is_set() else None

    def close(self):
        self.cap.release()
