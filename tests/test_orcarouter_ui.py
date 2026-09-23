"""Settings-window wiring for OrcaRouter, exercised headlessly.

The real window is AppKit on macOS. This file substitutes a minimal AppKit/Foundation stub
so `SettingsController.build()` actually runs here: the assertions are about the wiring —
which controls the OrcaRouter page creates, what the model combo is bound to, and what
happens to a login attempt when the page is left — not about pixels. Pixels are the macOS
probe's job (`probe/settings_smoke.py`), which this file does not replace.

No network and no real credential: the catalog and the PKCE server are local fakes.
"""
import json
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import orcarouter  # noqa: E402

FAKE_KEY = "sk-orca-ui-wiring-test-key"


class _Widget:
    """Enough of NSView/NSControl for the settings window to build and be inspected."""

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def __getattr__(self, name):
        """AppKit's surface is large; unknown `initWith…`/`set…`/`add…` calls are no-ops.

        Anything else still raises, so a typo in the test or in settings.py is not silently
        swallowed — only the Objective-C boilerplate this stub does not care about is.
        """
        if name.startswith(("initWith", "init", "set", "add", "remove", "insert", "make")):
            return lambda *args, **kwargs: self
        raise AttributeError(name)

    def __init__(self, *args, **kwargs):
        self._value = ""
        self._items = []
        self._enabled = True
        self._hidden = False
        self._subviews = []
        self._tooltip = ""
        self._label = ""
        self._frame = (0, 0, 0, 0)
        self._actions = []
        self._target = None

    # geometry / hierarchy
    def setFrame_(self, rect):
        self._frame = rect

    def frame(self):
        return self._frame

    def addSubview_(self, view):
        self._subviews.append(view)

    def subviews(self):
        return list(self._subviews)

    def setWantsLayer_(self, flag):
        pass

    def layer(self):
        return _Widget()

    def setAccessibilityLabel_(self, text):
        self._label = text

    def setToolTip_(self, text):
        self._tooltip = text

    def toolTip(self):
        return self._tooltip

    def accessibilityLabel(self):
        return self._label

    # text controls
    def setStringValue_(self, value):
        self._value = str(value)

    def stringValue(self):
        return self._value

    def setPlaceholderString_(self, value):
        self._placeholder = value

    def setCompletes_(self, flag):
        pass

    def setDelegate_(self, delegate):
        self._delegate = delegate

    def cell(self):
        return _Widget()

    def setWraps_(self, flag):
        pass

    # combo box
    def removeAllItems(self):
        self._items = []

    def addItemsWithObjectValues_(self, values):
        self._items.extend(values)

    def objectValues(self):
        return list(self._items)

    def setEnabled_(self, flag):
        self._enabled = flag

    def isEnabled(self):
        return self._enabled

    def setHidden_(self, flag):
        self._hidden = flag

    def isHidden(self):
        return self._hidden

    # buttons
    def setTitle_(self, text):
        self._value = text

    def title(self):
        return self._value

    def setTarget_(self, target):
        self._target = target

    def setAction_(self, action):
        self._actions.append(action)

    def setTag_(self, tag):
        self._tag = tag

    def tag(self):
        return getattr(self, "_tag", 0)

    def performClick_(self, sender):
        action = self._actions[-1] if self._actions else None
        if action and self._target is not None:
            getattr(self._target, action)(self)

    def setFont_(self, font):
        pass

    def setTextColor_(self, color):
        pass

    def setBackgroundColor_(self, color):
        pass

    def setBordered_(self, flag):
        pass

    def setContentTintColor_(self, color):
        pass

    def setImage_(self, image):
        pass

    def setImagePosition_(self, position):
        pass

    def setImageScaling_(self, scale):
        pass

    def setButtonType_(self, kind):
        pass

    def setLevel_(self, level):
        pass

    def setReleasedWhenClosed_(self, flag):
        pass

    def setAppearance_(self, appearance):
        pass

    def setOpaque_(self, flag):
        pass

    def setHasShadow_(self, flag):
        pass

    def setMaterial_(self, material):
        pass

    def setBlendingMode_(self, mode):
        pass

    def setState_(self, state):
        pass

    def setContentView_(self, view):
        self._content = view

    def contentView(self):
        return self._content

    def setDrawsBackground_(self, flag):
        pass

    def makeKeyAndOrderFront_(self, sender):
        pass

    def makeFirstResponder_(self, responder):
        pass

    def center(self):
        pass

    def close(self):
        pass

    def setIdentifier_(self, identifier):
        self._identifier = identifier

    def identifier(self):
        return getattr(self, "_identifier", None)

    def setView_(self, view):
        self._view = view

    def view(self):
        return self._view

    def setLabel_(self, text):
        self._value = text

    def label(self):
        return self._value

    def setSelectionRange_(self, rng):
        pass


class _TabView(_Widget):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self._items = []

    def addTabViewItem_(self, item):
        self._items.append(item)

    def tabViewItems(self):
        return list(self._items)

    def selectTabViewItemAtIndex_(self, index):
        self._selected = index

    def selectedTabViewItem(self):
        return self._items[getattr(self, "_selected", 0)]


class _App:
    def setActivationPolicy_(self, policy):
        pass

    def activateIgnoringOtherApps_(self, flag):
        pass

    def setDelegate_(self, delegate):
        self._delegate = delegate

    def run(self):
        pass


def _install_stubs():
    """A tiny AppKit/Foundation/objc so settings.py imports and builds off macOS.

    Only installed when the real pyobjc modules are unavailable, and removed again in
    tearDownClass: a stub left in `sys.modules` would silently replace AppKit for every
    later test file in the same `unittest discover` process.
    """
    try:
        import AppKit  # noqa: F401
        import Foundation  # noqa: F401
        return None, False
    except ImportError:
        pass

    installed = {}

    appkit = types.ModuleType("AppKit")

    class _NSColor(_Widget):
        @classmethod
        def clearColor(cls):
            return cls()

        @classmethod
        def whiteColor(cls):
            return cls()

        @classmethod
        def colorWithCalibratedRed_green_blue_alpha_(cls, *a):
            return cls()

        def colorWithAlphaComponent_(self, alpha):
            return self

        def CGColor(self):
            return "cg"

    class _NSFont(_Widget):
        @classmethod
        def systemFontOfSize_(cls, size):
            return cls()

        @classmethod
        def boldSystemFontOfSize_(cls, size):
            return cls()

    class _NSAlert(_Widget):
        NSAlertFirstButtonReturn = 1000
        NSAlertSecondButtonReturn = 1001

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setMessageText_(self, text):
            pass

        def setInformativeText_(self, text):
            pass

        def addButtonWithTitle_(self, title):
            pass

        def runModal(self):
            return 1001

    class _Pasteboard:
        @classmethod
        def generalPasteboard(cls):
            return cls()

        def clearContents(self):
            pass

        def setString_forType_(self, text, kind):
            pass

    class _Appearance:
        @classmethod
        def appearanceNamed_(cls, name):
            return cls()

    appkit.NSObject = _Widget
    appkit.NSWindow = _Widget
    appkit.NSView = _Widget
    appkit.NSVisualEffectView = _Widget
    appkit.NSTabView = _TabView
    appkit.NSTabViewItem = _Widget
    appkit.NSButton = _Widget
    appkit.NSSecureTextField = _Widget
    appkit.NSTextField = _Widget
    appkit.NSComboBox = _Widget
    appkit.NSFont = _NSFont
    appkit.NSColor = _NSColor
    appkit.NSAlert = _NSAlert
    appkit.NSPasteboard = _Pasteboard
    appkit.NSPasteboardTypeString = "public.utf8-plain-text"
    appkit.NSApplication = type("NSApplication", (), {
        "sharedApplication": staticmethod(lambda: _App()),
        "NSApplicationActivationPolicyRegular": 0,
        "NSApplicationActivationPolicyAccessory": 1,
    })
    appkit.NSAppearance = _Appearance
    appkit.NSAppearanceNameAqua = "NSAppearanceNameAqua"
    appkit.NSImage = _Widget
    appkit.NSImageScaleNone = 0
    appkit.NSImageOnly = 1
    appkit.NSWindowStyleMaskTitled = 1
    appkit.NSWindowStyleMaskClosable = 2
    appkit.NSBackingStoreBuffered = 2
    appkit.NSFloatingWindowLevel = 3
    appkit.NSVisualEffectMaterialSidebar = 4
    appkit.NSVisualEffectBlendingModeBehindWindow = 0
    appkit.NSVisualEffectStateActive = 1
    appkit.NSBitmapImageFileTypePNG = 4
    appkit.NSApplicationActivationPolicyRegular = 0

    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _Widget
    foundation.NSMakeRect = lambda x, y, w, h: (x, y, w, h)
    foundation.NSDate = _Widget

    objc = types.ModuleType("objc")
    objc.python_method = lambda fn: fn

    for name, module in (("AppKit", appkit), ("Foundation", foundation), ("objc", objc)):
        if name not in sys.modules:
            sys.modules[name] = module
            installed[name] = module
    return installed, True


class FakeCatalogServer:
    RECORDS = [
        {"id": "openai/gpt-5.5", "supported_endpoint_types": ["openai"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "google/gemini-3.5-flash", "supported_endpoint_types": ["openai"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "vendor/image-xl", "supported_endpoint_types": ["image-generation"]},
    ]

    def __init__(self, status=200):
        self.status = status
        self.paths = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"data": outer.RECORDS}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class SettingsWindowWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installed, cls.stubbed = _install_stubs()
        import settings
        cls.settings = settings

    @classmethod
    def tearDownClass(cls):
        for name, module in (cls.installed or {}).items():
            if sys.modules.get(name) is module:
                del sys.modules[name]
        # settings was imported against the stub; drop it so nothing later reuses it.
        if cls.stubbed:
            sys.modules.pop("settings", None)

    def _controller(self, tmpdir, env_text=""):
        path = Path(tmpdir) / "env"
        path.write_text(env_text)
        patches = [
            mock.patch.object(self.settings.userconfig, "_startup_sources", None),
            mock.patch.object(self.settings.userconfig, "env_files", return_value=[path]),
            mock.patch.object(self.settings.userconfig, "PROJECT_ENV", Path(tmpdir) / ".env"),
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(orcarouter, "catalog",
                              return_value=orcarouter.ModelCatalog(cache_ttl=0)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.settings.userconfig.load()
        controller = self.settings.SettingsController.alloc().init().build()
        self.path = path
        return controller

    def test_the_page_exposes_both_authentication_entrances(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            fields = controller.fields["ORCAROUTER"]
            # Entrance 1: a masked key field the user can paste into.
            self.assertIsNotNone(fields["API_KEY"])
            self.assertEqual(fields["API_KEY"].accessibilityLabel(),
                             "生成 · OrcaRouter 密钥")
            # Entrance 2: a distinct Connect button, not a second meaning for the first.
            self.assertEqual(controller.orca_login_button.title(),
                             "Connect with OrcaRouter")
            self.assertIsNotNone(controller.orca_cancel_button)
            self.assertTrue(controller.orca_login_button.isEnabled())
            self.assertFalse(controller.orca_cancel_button.isEnabled())

    def test_the_model_control_has_no_free_text_default(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            combo = controller.fields["ORCAROUTER"]["MODEL"]
            # Nothing usable until the catalog answers: the placeholder the combo shows is
            # the app's generic "暂无" hint, not a hand-written list of example models.
            self.assertEqual(combo.stringValue(), "")
            self.assertEqual([v for v in combo.objectValues() if "/" in str(v)], [])

    def test_the_catalog_binds_the_dropdown_options(self):
        import tempfile
        server = FakeCatalogServer()
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp, f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            controller.fields["ORCAROUTER"]["BASE_URL"].setStringValue_(server.base)
            controller.refresh_orca_catalog()
            combo = controller.fields["ORCAROUTER"]["MODEL"]
            self.assertEqual(sorted(combo.objectValues()),
                             ["google/gemini-3.5-flash", "openai/gpt-5.5"])
            self.assertNotIn("vendor/image-xl", combo.objectValues())
            self.assertEqual(server.paths[0], "/models?capability=chat")

    def test_a_capability_change_refilters_and_clears_an_incompatible_choice(self):
        import tempfile
        server = FakeCatalogServer()
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            controller.fields["ORCAROUTER"]["BASE_URL"].setStringValue_(server.base)
            controller.refresh_orca_catalog("chat")
            combo = controller.fields["ORCAROUTER"]["MODEL"]
            combo.setStringValue_("openai/gpt-5.5")
            # Same catalog, now asking for image-capable chat models only.
            controller.refresh_orca_catalog("chat", "image")
            self.assertEqual(combo.objectValues(), ["google/gemini-3.5-flash"])
            self.assertEqual(combo.stringValue(), "")
            self.assertIn("已清空", controller.status.stringValue())

    def test_a_degraded_catalog_says_so_instead_of_going_free_text(self):
        import tempfile
        server = FakeCatalogServer(status=503)
        self.addCleanup(server.stop)
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            controller.fields["ORCAROUTER"]["BASE_URL"].setStringValue_(server.base)
            controller.refresh_orca_catalog()
            combo = controller.fields["ORCAROUTER"]["MODEL"]
            self.assertTrue(combo.objectValues())
            self.assertTrue(set(combo.objectValues())
                            <= {m["id"] for m in orcarouter.SEED_MODELS})
            self.assertIn("备用清单", controller.status.stringValue())

    def test_leaving_the_page_releases_an_in_flight_login(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            session = controller.orca_login
            generation = session.begin()
            controller.orca_login_button.setEnabled_(False)
            controller.orca_cancel_button.setEnabled_(True)

            class _Item:
                def identifier(self):
                    return "OPENAI"

            controller.tabView_didSelectTabViewItem_(controller.tabs, _Item())
            self.assertFalse(session.busy)
            self.assertGreater(session.generation, generation)
            self.assertFalse(session.current(generation))
            self.assertTrue(controller.orca_login_button.isEnabled())
            self.assertFalse(controller.orca_cancel_button.isEnabled())
            self.assertEqual(controller.orca_status.stringValue(), "")

    def test_a_stale_login_response_cannot_touch_the_newer_attempt(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            session = controller.orca_login
            first = session.begin()
            session.release()                      # user cancelled / left the page
            second = session.begin()
            controller.orca_status.setStringValue_("第二次登录中")
            # A late response from the first attempt must be ignored outright.
            controller.orcaLoginFinished_({"generation": first, "error": "过期失败"})
            self.assertEqual(controller.orca_status.stringValue(), "第二次登录中")
            self.assertTrue(session.busy)
            self.assertTrue(session.current(second))
            # The attempt that owns the UI still lands normally.
            controller.orcaLoginFinished_({"generation": second, "cancelled": True})
            self.assertFalse(session.busy)
            self.assertEqual(controller.orca_status.stringValue(), "已取消登录。")

    def test_explicit_cancel_clears_state_and_lets_a_second_login_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            session = controller.orca_login
            session.begin()
            controller.orca_cancel_button.setEnabled_(True)
            controller.orcaLogin_ = None            # not clicked; only the cancel path runs
            controller.orcaCancel_(None)
            self.assertFalse(session.busy)
            self.assertTrue(controller.orca_login_button.isEnabled())
            self.assertFalse(controller.orca_cancel_button.isEnabled())
            self.assertEqual(controller.orca_status.stringValue(), "已取消登录。")
            # A second login can start without rebuilding the window.
            generation = session.begin()
            self.assertTrue(session.current(generation))

    def test_window_close_releases_the_login(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            session = controller.orca_login
            session.begin()
            controller.windowWillClose_(None)
            self.assertFalse(session.busy)

    def test_termination_releases_the_login(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            session = controller.orca_login
            session.begin()
            controller.applicationWillTerminate_(None)
            self.assertFalse(session.busy)

    def test_a_signed_in_credential_is_written_and_reflected_in_the_form(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            controller.fields["ORCAROUTER"]["BASE_URL"].setStringValue_(
                "https://api.orcarouter.ai/v1")
            credential = orcarouter.Credential(key=FAKE_KEY, source="pkce",
                                               account="4242", scope="api")
            controller.persist_orca_credential(credential)
            written = self.path.read_text()
            self.assertIn(FAKE_KEY, written)
            self.assertIn("ORCAROUTER_ACCOUNT_ID", written)
            self.assertIn('export ORCAROUTER_BASE_URL=https://api.orcarouter.ai/v1', written)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(controller.fields["ORCAROUTER"]["API_KEY"].stringValue(),
                             FAKE_KEY)
            self.assertEqual(controller.fields["ORCAROUTER"]["MODEL"].stringValue(), "")

    def test_the_status_line_never_shows_a_whole_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, f"ORCAROUTER_API_KEY={FAKE_KEY}\n")
            summary, source = controller.current_source("ORCAROUTER")
            self.assertNotIn(FAKE_KEY, summary)
            self.assertNotIn(FAKE_KEY, source)
            self.assertIn("OrcaRouter", summary)

    def test_other_provider_pages_keep_their_own_model_entry(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp)
            self.assertIn("暂无", controller.fields["OPENAI"]["MODEL"].objectValues())


if __name__ == "__main__":
    unittest.main()
