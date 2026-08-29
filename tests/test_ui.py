"""The window, checked without a display.

Qt is not installed on every machine that runs these tests, and there is never
a display, so PySide6 is replaced by a permissive stand-in. That is enough to
import every UI module — which catches typos, bad names and broken class bodies
— and to test the parts that are ordinary Python: the palette, the translucency
maths, and the settings form's type handling.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_qt_stub() -> bool:
    """Put a fake PySide6 on sys.modules. False if the real one is there."""
    try:
        import PySide6  # noqa: F401
        return False
    except ImportError:
        pass

    class Any:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return Any()

        def __call__(self, *a, **k):
            return Any()

        def __or__(self, other):
            return self

        __ror__ = __or__

        def __eq__(self, other):
            return False

        def __hash__(self):
            return id(self)

    class Meta(type):
        def __getattr__(cls, name):
            return Any()

    class Base(metaclass=Meta):
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return Any()

    class Module(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return type(name, (Base,), {})

    package = types.ModuleType("PySide6")
    package.__path__ = []
    sys.modules["PySide6"] = package
    for name in ("QtCore", "QtGui", "QtWidgets"):
        module = Module(f"PySide6.{name}")
        sys.modules[f"PySide6.{name}"] = module
        setattr(package, name, module)
    return True


_STUBBED = _install_qt_stub()


class TestUiImports(unittest.TestCase):
    """Every UI module must import. Class bodies run here, so typos surface."""

    def test_every_window_module_imports(self):
        import importlib

        for name in ("toony.ui", "toony.ui.theme", "toony.ui.avatar",
                     "toony.ui.client", "toony.ui.window", "toony.ui.settings",
                     "toony.ui.main"):
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_the_package_reports_whether_qt_is_present(self):
        import toony.ui

        self.assertIsInstance(toony.ui.available(), bool)


class TestSingleInstanceDoorbell(unittest.TestCase):
    """A second launch has to lend its activation token to the running window."""

    def setUp(self):
        import os
        import tempfile

        self.previous = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tempfile.mkdtemp()
        self.addCleanup(self._restore)

    def _restore(self):
        import os

        if self.previous is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.previous

    def test_the_token_crosses_to_the_running_instance(self):
        import os
        import time

        from toony.ui.main import SingleInstance

        seen = []
        first = SingleInstance(on_show=seen.append)
        self.assertTrue(first.claim())
        self.addCleanup(first.release)

        os.environ["XDG_ACTIVATION_TOKEN"] = "kwin-token-xyz"
        self.addCleanup(lambda: os.environ.pop("XDG_ACTIVATION_TOKEN", None))
        second = SingleInstance(on_show=lambda token: None)
        self.assertFalse(second.claim(), "a second window should not open")

        deadline = time.monotonic() + 3.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(seen, ["kwin-token-xyz"])

    def test_no_token_is_not_an_error(self):
        import os
        import time

        from toony.ui.main import SingleInstance

        os.environ.pop("XDG_ACTIVATION_TOKEN", None)
        os.environ.pop("DESKTOP_STARTUP_ID", None)
        seen = []
        first = SingleInstance(on_show=seen.append)
        self.assertTrue(first.claim())
        self.addCleanup(first.release)
        SingleInstance(on_show=lambda token: None).claim()

        deadline = time.monotonic() + 3.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(seen, [""])


class TestTheme(unittest.TestCase):
    def setUp(self):
        from toony.ui import theme

        self.theme = theme

    def test_a_hex_colour_becomes_rgba(self):
        self.assertEqual(self.theme.rgba("#16161d", 0.8), "rgba(22, 22, 29, 0.800)")

    def test_short_hex_is_expanded(self):
        self.assertEqual(self.theme.rgba("#fff", 1.0), "rgba(255, 255, 255, 1.000)")

    def test_nonsense_is_returned_unchanged(self):
        self.assertEqual(self.theme.rgba("not a colour", 0.5), "not a colour")

    def test_alpha_is_clamped(self):
        self.assertIn("1.000", self.theme.rgba("#000", 5.0))
        self.assertIn("0.000", self.theme.rgba("#000", -1.0))

    def test_opacity_is_painted_into_the_backgrounds(self):
        """Wayland ignores setWindowOpacity, so the colours have to carry it."""
        sheet = self.theme.stylesheet("dark", opacity=0.8)
        self.assertIn("rgba(", sheet)

    def test_a_fully_opaque_sheet_uses_plain_hex(self):
        sheet = self.theme.stylesheet("dark", opacity=1.0)
        self.assertIn("#16161d", sheet)
        self.assertNotIn("rgba(22, 22, 29", sheet)

    def test_both_palettes_define_every_colour(self):
        self.assertEqual(set(self.theme.DARK), set(self.theme.LIGHT))

    def test_the_sheet_has_no_unfilled_placeholders(self):
        """CSS braces are fine; a leftover {name} means a missing colour."""
        import re

        for mode in ("dark", "light"):
            sheet = self.theme.stylesheet(mode, font_size=13, opacity=0.9)
            self.assertEqual(re.findall(r"\{[a-z_]+\}", sheet), [], mode)

    def test_an_explicit_mode_is_honoured(self):
        self.assertEqual(self.theme.resolve("light"), "light")
        self.assertEqual(self.theme.resolve("dark"), "dark")


class TestOrbStates(unittest.TestCase):
    """The ring is the whole point: each state must look different."""

    def setUp(self):
        from toony.ui import theme

        self.theme = theme

    def test_every_state_has_a_colour(self):
        for state in ("idle", "starting", "listening", "thinking", "speaking",
                      "offline"):
            with self.subTest(state=state):
                colours = self.theme.state_colours(state)
                self.assertTrue(colours["ring"].startswith("#"))
                self.assertGreater(colours["alpha"], 0)

    def test_busy_states_are_distinguishable_from_each_other(self):
        rings = {s: self.theme.state_colours(s)["ring"]
                 for s in ("idle", "listening", "thinking", "speaking")}
        self.assertEqual(len(set(rings.values())), 4, rings)

    def test_listening_takes_the_accent_colour(self):
        self.assertEqual(self.theme.state_colours("listening", "#ff0000")["ring"],
                         "#ff0000")

    def test_an_unknown_state_falls_back_to_idle(self):
        self.assertEqual(self.theme.state_colours("nonsense"),
                         self.theme.state_colours("idle"))

    def test_idle_is_dimmer_than_listening(self):
        self.assertLess(self.theme.state_colours("idle")["alpha"],
                        self.theme.state_colours("listening")["alpha"])


class TestOrbGeometry(unittest.TestCase):
    """The arc maths, which is ordinary Python and worth checking."""

    def _orb(self, state="idle"):
        from toony.config import Config
        from toony.ui.orb import Orb

        orb = Orb.__new__(Orb)
        orb.config = Config()
        orb.state = state
        orb._phase = 0.25
        orb._level = 1.0
        return orb

    def test_idle_draws_no_bright_arc(self):
        self.assertEqual(self._orb("idle")._span(), 0.0)

    def test_speaking_fills_the_whole_ring(self):
        self.assertEqual(self._orb("speaking")._span(), 360.0)

    def test_thinking_chases_a_quarter_around(self):
        orb = self._orb("thinking")
        self.assertEqual(orb._span(), 90.0)
        first = orb._start()
        orb._phase += 0.5
        self.assertNotEqual(orb._start(), first)

    def test_listening_grows_but_never_overflows(self):
        orb = self._orb("listening")
        for phase in (0.0, 0.25, 0.5, 0.75, 1.0):
            orb._phase = phase
            with self.subTest(phase=phase):
                self.assertGreaterEqual(orb._span(), 0.0)
                self.assertLessEqual(orb._span(), 360.0)

    def test_the_pulse_stays_between_zero_and_one(self):
        for state in ("idle", "listening", "thinking", "speaking"):
            orb = self._orb(state)
            for phase in (0.0, 0.3, 0.7, 1.4, 9.9):
                orb._phase = phase
                with self.subTest(state=state, phase=phase):
                    self.assertGreaterEqual(orb._pulse(), 0.0)
                    self.assertLessEqual(orb._pulse(), 1.0)


class TestSettingsHints(unittest.TestCase):
    def setUp(self):
        from toony.config import Config
        from toony.ui import settings

        self.settings = settings
        self.config = Config()

    def test_every_hint_names_a_real_setting(self):
        """A hint for a key that no longer exists is a silent no-op."""
        flat = self.config.flatten()
        unknown = [key for key in self.settings.HINTS if key not in flat]
        self.assertEqual(unknown, [], f"stale hints: {unknown}")

    def test_every_choice_list_contains_the_default(self):
        for key, (choices, _) in self.settings.HINTS.items():
            if not isinstance(choices, list):
                continue
            with self.subTest(key=key):
                self.assertIn(str(self.config.get(key)), [str(c) for c in choices])

    def test_every_range_contains_the_default(self):
        for key, (bounds, _) in self.settings.HINTS.items():
            if not isinstance(bounds, tuple):
                continue
            value = self.config.get(key)
            with self.subTest(key=key):
                self.assertGreaterEqual(value, bounds[0])
                self.assertLessEqual(value, bounds[1])

    def test_every_setting_lands_on_exactly_one_tab(self):
        prefixes = [p for _, group in self.settings.GROUPS for p in group]
        self.assertEqual(len(prefixes), len(set(prefixes)),
                         "a section is listed under two tabs")

    def test_secrets_are_masked_in_the_form(self):
        for key in ("brain.claude.api_key", "telegram.token"):
            with self.subTest(key=key):
                self.assertTrue(key.endswith(self.settings._SECRET))


if __name__ == "__main__":
    unittest.main()
