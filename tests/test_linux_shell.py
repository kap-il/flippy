"""The Linux app window's navigation guard and saved size (flippy/linux/shell_window.py), without a display."""
import os
import pathlib
import sys
import tempfile
import unittest

LINUX = sys.platform.startswith("linux")


@unittest.skipUnless(LINUX, "Linux only")
class NavigationTest(unittest.TestCase):
    def setUp(self):
        from flippy.linux import shell_window
        self.sw = shell_window
        self.web = shell_window.WEB

    def test_the_page_and_its_files_load(self):
        index = pathlib.Path(self.web, "index.html").as_uri()
        for uri in (index, index + "#settings/appearance", pathlib.Path(self.web, "app.js").as_uri(),
                    "file://localhost" + pathlib.Path(self.web, "app.css").as_uri()[len("file://"):]):
            self.assertEqual(self.sw.navigation(uri), "allow", uri)

    def test_other_files_are_ignored(self):
        outside = [
            "file:///etc/passwd",
            pathlib.Path(self.web).as_uri(),                                  # the folder itself
            pathlib.Path(self.web).as_uri() + "/../bridge.py",                # climbing out
            pathlib.Path(self.web).as_uri() + "/%2e%2e/bridge.py",            # climbing out, encoded
            pathlib.Path(self.web + "-evil", "index.html").as_uri(),           # a sibling sharing the prefix
            "file://otherhost" + pathlib.Path(self.web, "index.html").as_uri()[len("file://"):],
        ]
        for uri in outside:
            self.assertEqual(self.sw.navigation(uri), "ignore", uri)

    def test_web_links_open_in_the_browser(self):
        for uri in ("https://example.com", "http://example.com/a?b#c", "HTTPS://Example.com"):
            self.assertEqual(self.sw.navigation(uri), "open", uri)

    def test_everything_else_is_ignored(self):
        for uri in ("about:blank", "javascript:alert(1)", "data:text/html,hi", "blob:file:///x", "ftp://x/y",
                    "https://", "flippy://x", "", None, "http://[::1"):
            self.assertEqual(self.sw.navigation(uri), "ignore", uri)

    def test_a_symlink_out_of_the_page_folder_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            web = os.path.join(tmp, "web")
            os.mkdir(web)
            os.symlink("/etc/passwd", os.path.join(web, "link"))
            self.assertEqual(self.sw.navigation(pathlib.Path(web, "link").as_uri(), web), "ignore")


@unittest.skipUnless(LINUX, "Linux only")
class SizeTest(unittest.TestCase):
    def test_size_round_trips_and_has_a_floor(self):
        from flippy.linux import shell_window as sw
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "flippy", sw.SIZE_FILE)
            self.assertEqual(sw.load_size(path), (sw.WIDTH, sw.HEIGHT))
            sw.save_size(900, 700, path)
            self.assertEqual(sw.load_size(path), (900, 700))
            sw.save_size(10, 10, path)
            self.assertEqual(sw.load_size(path), (sw.MIN_WIDTH, sw.MIN_HEIGHT))
            with open(path, "w") as f:
                f.write("not json")
            self.assertEqual(sw.load_size(path), (sw.WIDTH, sw.HEIGHT))


if __name__ == "__main__":
    unittest.main()
