import contextlib
import ctypes as C
import types
import unittest

from wxagentbot.capture import (CaptureUnavailable, CompositeCapture, Visual,
                                   WindowAttributes, WindowPixels, XImage, decode_image)


class CaptureTests(unittest.TestCase):
    def image(self):
        # Two RGB pixels per row, padded to 12 bytes by the X server.
        raw = C.create_string_buffer(bytes([30, 20, 10, 0, 60, 50, 40, 0, 0, 0, 0, 0]) * 2)
        image = XImage(width=2, height=2, format=2, data=C.cast(raw, C.c_void_p),
                       depth=24, bits_per_pixel=32, bytes_per_line=12, byte_order=0)
        return image, raw

    def test_native_pixels_respect_stride_and_channel_order(self):
        image, raw = self.image()
        result = decode_image(image, (0xff0000, 0xff00, 0xff))
        self.assertEqual(result.getpixel((0, 1)), (10, 20, 30))
        self.assertEqual(result.getpixel((1, 1)), (40, 50, 60))

    def test_unexpected_layout_is_rejected_before_reading_memory(self):
        image, raw = self.image()
        for field, bad in (("bytes_per_line", 1), ("depth", 8), ("width", 0),
                           ("bits_per_pixel", 16), ("byte_order", 2)):
            original = getattr(image, field)
            setattr(image, field, bad)
            with self.subTest(field=field), self.assertRaises(CaptureUnavailable):
                decode_image(image, (0xff0000, 0xff00, 0xff))
            setattr(image, field, original)

    def test_window_pixels_translate_negative_screen_coordinates(self):
        image, raw = self.image()
        pixels = WindowPixels(decode_image(image, (0xff0000, 0xff00, 0xff)), -50, 30)
        self.assertEqual(pixels.bounds, (-50, 30, -48, 32))
        self.assertEqual(pixels.getpixel((-49, 31)), (40, 50, 60))

    def backend(self, *, minimized=False, image_missing=False):
        capture = CompositeCapture()
        capture.display = 1
        capture._init = lambda: None
        capture._errors = lambda: contextlib.nullcontext([])
        calls = []
        image, raw = self.image()
        visual = Visual(red_mask=0xff0000, green_mask=0xff00, blue_mask=0xff)
        def attrs(display, window, ptr):
            a = C.cast(ptr, C.POINTER(WindowAttributes)).contents
            a.width, a.height, a.root, a.map_state = 2, 2, 1, 0 if minimized else 2
            a.visual = C.pointer(visual)
            return 1
        def translate(display, window, root, zero_x, zero_y, x, y, child):
            C.cast(x, C.POINTER(C.c_int)).contents.value = 100
            C.cast(y, C.POINTER(C.c_int)).contents.value = 200
            return 1
        capture.x = types.SimpleNamespace(
            XGetWindowAttributes=attrs, XTranslateCoordinates=translate,
            XSync=lambda *args: None,
            XGetImage=lambda *args: None if image_missing else C.pointer(image),
            XDestroyImage=lambda *args: calls.append("destroy_image"),
            XFreePixmap=lambda *args: calls.append("free_pixmap"))
        capture.composite = types.SimpleNamespace(
            XCompositeRedirectWindow=lambda *args: calls.append("redirect"),
            XCompositeNameWindowPixmap=lambda *args: 777)
        # Keep the native buffers alive for the duration of the test.
        capture.buffers = (raw, image, visual)
        return capture, calls

    def test_background_capture_reads_window_storage_and_frees_resources(self):
        capture, calls = self.backend()
        pixels = capture.capture("20")
        self.assertEqual(pixels.getpixel((101, 201)), (40, 50, 60))
        self.assertEqual(calls, ["redirect", "destroy_image", "free_pixmap"])
        capture.capture("20")
        self.assertEqual(calls.count("redirect"), 1)

    def test_minimized_window_is_reported_without_mapping_or_activation(self):
        capture, calls = self.backend(minimized=True)
        with self.assertRaisesRegex(CaptureUnavailable, "最小化"):
            capture.capture("20")
        self.assertEqual(calls, [])

    def test_failed_get_image_still_frees_the_pixmap(self):
        capture, calls = self.backend(image_missing=True)
        with self.assertRaises(CaptureUnavailable):
            capture.capture("20")
        self.assertEqual(calls, ["redirect", "free_pixmap"])
