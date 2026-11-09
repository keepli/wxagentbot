"""Read an X11 window's own pixels without raising it or moving the pointer."""
from __future__ import annotations

import contextlib
import ctypes as C
import ctypes.util
import os
import threading


class CaptureUnavailable(RuntimeError):
    pass


class Visual(C.Structure):
    _fields_ = [("ext_data", C.c_void_p), ("visualid", C.c_ulong), ("visual_class", C.c_int),
                ("red_mask", C.c_ulong), ("green_mask", C.c_ulong), ("blue_mask", C.c_ulong),
                ("bits_per_rgb", C.c_int), ("map_entries", C.c_int)]


class WindowAttributes(C.Structure):
    _fields_ = [(name, C.c_int) for name in ("x", "y", "width", "height", "border_width", "depth")] + [
        ("visual", C.POINTER(Visual)), ("root", C.c_ulong),
        ("window_class", C.c_int), ("bit_gravity", C.c_int), ("win_gravity", C.c_int),
        ("backing_store", C.c_int), ("backing_planes", C.c_ulong), ("backing_pixel", C.c_ulong),
        ("save_under", C.c_int), ("colormap", C.c_ulong), ("map_installed", C.c_int),
        ("map_state", C.c_int), ("all_event_masks", C.c_long), ("your_event_mask", C.c_long),
        ("do_not_propagate_mask", C.c_long), ("override_redirect", C.c_int), ("screen", C.c_void_p)]


class XImage(C.Structure):
    _fields_ = [("width", C.c_int), ("height", C.c_int), ("xoffset", C.c_int), ("format", C.c_int),
                ("data", C.c_void_p)] + [(name, C.c_int) for name in (
                    "byte_order", "bitmap_unit", "bitmap_bit_order", "bitmap_pad", "depth",
                    "bytes_per_line", "bits_per_pixel")] + [
        ("red_mask", C.c_ulong), ("green_mask", C.c_ulong), ("blue_mask", C.c_ulong),
        ("obdata", C.c_void_p), ("functions", C.c_void_p * 6)]


class XError(C.Structure):
    _fields_ = [("type", C.c_int), ("display", C.c_void_p), ("resourceid", C.c_ulong),
                ("serial", C.c_ulong), ("error_code", C.c_ubyte),
                ("request_code", C.c_ubyte), ("minor_code", C.c_ubyte)]


class WindowPixels:
    """Expose pixels using AT-SPI's screen coordinates, including negative origins."""
    def __init__(self, image, x, y):
        self.image = image
        self.bounds = (x, y, x + image.width, y + image.height)
        self.width, self.height = x + image.width, y + image.height

    def getpixel(self, xy):
        return self.image.getpixel((xy[0] - self.bounds[0], xy[1] - self.bounds[1]))


def decode_image(image, masks):
    from PIL import Image
    if ((image.width <= 0 or image.height <= 0 or image.width > 8192 or image.height > 8192)
            or image.format != 2 or image.xoffset != 0 or image.depth not in (24, 32)
            or masks != (0xff0000, 0xff00, 0xff) or image.bits_per_pixel not in (24, 32)
            or image.byte_order not in (0, 1) or not image.data):
        raise CaptureUnavailable("窗口像素格式不受支持，已停止该次静默读取")
    minimum = image.width * (image.bits_per_pixel // 8)
    length = image.bytes_per_line * image.height
    if image.bytes_per_line < minimum or not 0 < length <= 64 * 1024 * 1024:
        raise CaptureUnavailable("窗口像素尺寸无效")
    raw = C.string_at(image.data, length)
    mode = { (32, 0): "BGRX", (32, 1): "XRGB", (24, 0): "BGR", (24, 1): "RGB" }[
        image.bits_per_pixel, image.byte_order]
    return Image.frombytes("RGB", (image.width, image.height), raw, "raw", mode, image.bytes_per_line, 1)


_errors_lock = threading.Lock()
_handler_type = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(XError))


class CompositeCapture:
    def __init__(self):
        self.display = None
        self.redirected = set()

    def _init(self):
        if self.display:
            return
        try:
            self.x = C.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
            self.composite = C.CDLL(ctypes.util.find_library("Xcomposite") or "libXcomposite.so.1")
        except OSError as e:
            raise CaptureUnavailable("静默窗口模式需要 libX11 和 libXcomposite") from e
        specs = {
            "XOpenDisplay": ([C.c_char_p], C.c_void_p),
            "XCloseDisplay": ([C.c_void_p], C.c_int),
            "XSync": ([C.c_void_p, C.c_int], C.c_int),
            "XSetErrorHandler": ([C.c_void_p], C.c_void_p),
            "XGetWindowAttributes": ([C.c_void_p, C.c_ulong, C.POINTER(WindowAttributes)], C.c_int),
            "XTranslateCoordinates": ([C.c_void_p, C.c_ulong, C.c_ulong, C.c_int, C.c_int,
                                        C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_ulong)], C.c_int),
            "XGetImage": ([C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint,
                           C.c_ulong, C.c_int], C.POINTER(XImage)),
            "XDestroyImage": ([C.POINTER(XImage)], C.c_int),
            "XFreePixmap": ([C.c_void_p, C.c_ulong], C.c_int),
        }
        for name, (args, result) in specs.items():
            fn = getattr(self.x, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in (
            ("XCompositeQueryExtension", [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)], C.c_int),
            ("XCompositeQueryVersion", [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)], C.c_int),
            ("XCompositeRedirectWindow", [C.c_void_p, C.c_ulong, C.c_int], None),
            ("XCompositeUnredirectWindow", [C.c_void_p, C.c_ulong, C.c_int], None),
            ("XCompositeNameWindowPixmap", [C.c_void_p, C.c_ulong], C.c_ulong)):
            fn = getattr(self.composite, name)
            fn.argtypes, fn.restype = args, result
        self.display = self.x.XOpenDisplay(os.fsencode(os.environ.get("DISPLAY", "")))
        if not self.display:
            raise CaptureUnavailable("无法连接 X11，静默模式不会尝试激活窗口")
        a, b = C.c_int(), C.c_int()
        if (not self.composite.XCompositeQueryExtension(self.display, C.byref(a), C.byref(b))
                or not self.composite.XCompositeQueryVersion(self.display, C.byref(a), C.byref(b))
                or (a.value, b.value) < (0, 2)):
            self.close()
            raise CaptureUnavailable("X11 服务不支持 Composite 0.2，无法读取被遮挡窗口")

    @contextlib.contextmanager
    def _errors(self):
        errors = []
        with _errors_lock:
            @_handler_type
            def handler(display, event):
                if display == self.display:
                    errors.append(event.contents.error_code)
                elif previous:
                    return _handler_type(previous)(display, event)
                return 0
            previous = self.x.XSetErrorHandler(C.cast(handler, C.c_void_p))
            try:
                yield errors
            finally:
                self.x.XSync(self.display, 0)
                self.x.XSetErrorHandler(previous)

    def capture(self, window):
        self._init()
        window = int(window)
        attrs = WindowAttributes()
        pixmap, image = 0, None
        with self._errors() as errors:
            try:
                if not self.x.XGetWindowAttributes(self.display, window, C.byref(attrs)) or errors:
                    raise CaptureUnavailable("静默聊天窗口已关闭或无法读取")
                if attrs.map_state != 2:
                    raise CaptureUnavailable("静默聊天窗口被最小化，请恢复独立窗口；监听不会主动置前")
                if not attrs.visual or attrs.width <= 0 or attrs.height <= 0:
                    raise CaptureUnavailable("聊天窗口尚未完成绘制")
                if window not in self.redirected:
                    self.composite.XCompositeRedirectWindow(self.display, window, 0)
                    self.x.XSync(self.display, 0)
                    if errors:
                        raise CaptureUnavailable("无法获取窗口后台像素，监听不会改为前台点击")
                    self.redirected.add(window)
                x, y, child = C.c_int(), C.c_int(), C.c_ulong()
                if not self.x.XTranslateCoordinates(self.display, window, attrs.root, 0, 0,
                                                   C.byref(x), C.byref(y), C.byref(child)):
                    raise CaptureUnavailable("无法读取聊天窗口位置")
                pixmap = self.composite.XCompositeNameWindowPixmap(self.display, window)
                self.x.XSync(self.display, 0)
                if errors or not pixmap:
                    raise CaptureUnavailable("聊天窗口后台像素暂不可用")
                image = self.x.XGetImage(self.display, pixmap, 0, 0, attrs.width, attrs.height, C.c_ulong(-1).value, 2)
                self.x.XSync(self.display, 0)
                if errors or not image:
                    raise CaptureUnavailable("无法获取完整后台图像，请检查独立聊天窗口")
                visual = attrs.visual.contents
                pixels = decode_image(image.contents, (visual.red_mask, visual.green_mask, visual.blue_mask))
                return WindowPixels(pixels, x.value, y.value)
            finally:
                if image:
                    self.x.XDestroyImage(image)
                if pixmap:
                    self.x.XFreePixmap(self.display, pixmap)

    def close(self):
        if self.display:
            with self._errors():
                for window in self.redirected:
                    self.composite.XCompositeUnredirectWindow(self.display, window, 0)
            self.x.XCloseDisplay(self.display)
            self.display = None
            self.redirected.clear()
