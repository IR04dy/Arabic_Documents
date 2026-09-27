"""Checks of the complaints workspace UI (complaints_ui.js, complaints.css).

The behaviour tests live in test_complaints_ui.js — the script run by node over
a fake DOM built from ui.html — and are reported here one test per scenario;
the stylesheet checks (WCAG contrast of small status text, the detail panel's
live region, no detail dialog left) are plain Python over the CSS, the script
and the ui.html markup and tokens."""
import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = shutil.which("node")


def _read(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(NODE, "node is not installed")
class ScriptBehaviourTests(unittest.TestCase):
    results: dict = {}
    output = ""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([NODE, os.path.join(HERE, "test_complaints_ui.js"), "--json"], cwd=HERE,
                              capture_output=True, timeout=300)
        cls.output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        cls.results = {}
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("{"):
                row = json.loads(line)
                cls.results[row["name"]] = row

    def check(self, name: str):
        row = self.results.get(name)
        self.assertIsNotNone(row, f"{name} did not run:\n{self.output[-3000:]}")
        self.assertTrue(row["ok"], row["error"])

    def test_every_scenario_passes(self):
        self.assertTrue(self.results, self.output[-3000:])
        for name, row in self.results.items():
            with self.subTest(name):
                self.assertTrue(row["ok"], row["error"])

    # F1: «تأكيد التصنيف» after editing the form.
    def test_confirm_with_edits_saves_correction(self):
        self.check("confirm_with_edits_saves_correction")

    def test_confirm_with_edits_cancelled_posts_nothing(self):
        self.check("confirm_with_edits_cancelled_posts_nothing")

    def test_clean_confirm_posts_confirm(self):
        self.check("clean_confirm_posts_confirm")

    # F2: session items older than the intake window.
    def test_offlist_session_item_is_followed_and_polling_stops(self):
        self.check("offlist_session_item_is_followed_and_polling_stops")

    def test_offlist_detail_follows_processing(self):
        self.check("offlist_detail_follows_processing")

    # F3 / open issue (a): the badge follows summary.outside_jurisdiction.
    def test_outside_badge_follows_server_flag(self):
        self.check("outside_badge_follows_server_flag")

    # F4: a save keeps the other form's draft and the focus.
    def test_saving_keeps_other_form_draft_and_focus(self):
        self.check("saving_keeps_other_form_draft_and_focus")

    # F5: live regions.
    def test_queue_chip_untouched_when_unchanged(self):
        self.check("queue_chip_untouched_when_unchanged")

    def test_empty_search_is_announced(self):
        self.check("empty_search_is_announced")

    # F6
    def test_upload_status_counts_browser_rejections(self):
        self.check("upload_status_counts_browser_rejections")

    # F9
    def test_txt_upload_hides_reextraction(self):
        self.check("txt_upload_hides_reextraction")

    # F10
    def test_page_one_citation_moves_pdf_back(self):
        self.check("page_one_citation_moves_pdf_back")

    # F11
    def test_counted_phrases_agree(self):
        self.check("counted_phrases_agree")

    # Open issue (c): «موقع المشكلة».
    def test_incident_location_field_renders(self):
        self.check("incident_location_field_renders")

    # Field review (cms_field_review_change §4): unmatched values wait in «تحتاج مراجعة».
    def test_pending_fields_section_lists_unmatched_values(self):
        self.check("pending_fields_section_lists_unmatched_values")

    def test_pending_field_accept_moves_focus_and_announces(self):
        self.check("pending_field_accept_moves_focus_and_announces")

    def test_pending_field_change_enter_saves_escape_cancels(self):
        self.check("pending_field_change_enter_saves_escape_cancels")

    def test_pending_field_error_keeps_row(self):
        self.check("pending_field_error_keeps_row")

    def test_near_source_link_highlights_the_approximate_span(self):
        self.check("near_source_link_highlights_the_approximate_span")

    def test_pending_fields_wait_while_processing(self):
        self.check("pending_fields_wait_while_processing")

    def test_pending_addressee_keeps_elsewhere_note(self):
        self.check("pending_addressee_keeps_elsewhere_note")

    def test_evidence_shows_every_quote(self):
        self.check("evidence_shows_every_quote")

    def test_register_tags_pending_fields(self):
        self.check("register_tags_pending_fields")

    def test_field_review_event_in_history(self):
        self.check("field_review_event_in_history")

    # The detail opens in place: an accordion row under the register row.
    def test_accordion_row_toggles_in_place(self):
        self.check("accordion_row_toggles_in_place")

    def test_one_complaint_expanded_at_a_time(self):
        self.check("one_complaint_expanded_at_a_time")

    def test_register_refresh_keeps_open_panel(self):
        self.check("register_refresh_keeps_open_panel")

    def test_register_reload_keeps_focus_on_the_trigger(self):
        self.check("register_reload_keeps_focus_on_the_trigger")

    def test_saving_reloads_the_register_without_folding(self):
        self.check("saving_reloads_the_register_without_folding")

    def test_deep_link_from_intake_expands_in_register(self):
        self.check("deep_link_from_intake_expands_in_register")

    def test_ref_chips_expand_in_register_searching_by_ref(self):
        self.check("ref_chips_expand_in_register_searching_by_ref")

    def test_collapse_button_and_escape_return_focus(self):
        self.check("collapse_button_and_escape_return_focus")

    def test_delete_folds_removes_row_and_focuses_next(self):
        self.check("delete_folds_removes_row_and_focuses_next")

    def test_panel_releases_resources_when_folded(self):
        self.check("panel_releases_resources_when_folded")

    def test_reprocess_in_panel_keeps_focus(self):
        self.check("reprocess_in_panel_keeps_focus")


def _tokens(block: str) -> dict:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;}]+)", block)}


def _block(text: str, opener: str) -> str:
    """The declarations of the first rule that starts with `opener` (up to its '}')."""
    start = text.index(opener) + len(opener)
    return text[start:text.index("}", start)]


def _palettes() -> dict:
    """{'light': tokens, 'dark': tokens} as #complaints-workspace sees them."""
    html, css = _read("ui.html"), _read("complaints.css")
    dark_html = html[html.index("@media (prefers-color-scheme:dark){:root{"):]
    dark_css = css[css.index("@media (prefers-color-scheme:dark){#complaints-workspace{"):]
    light = {**_tokens(_block(html, ":root{")), **_tokens(_block(css, "#complaints-workspace{"))}
    dark = {**light, **_tokens(_block(dark_html, ":root{")), **_tokens(_block(dark_css, "#complaints-workspace{"))}
    return {"light": light, "dark": dark}


def _resolve(value: str, tokens: dict) -> str:
    for _ in range(10):
        m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
        if not m:
            return value.strip()
        value = tokens[m.group(1)]
    raise AssertionError(f"unresolved {value}")


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _declaration(css: str, selector: str, prop: str) -> str | None:
    body = _block(css, "#complaints-workspace " + selector + "{")
    m = re.search(r"(?<![-\w])" + prop + r"\s*:\s*([^;}]+)", body)
    return m.group(1).strip() if m else None


class StylesheetTests(unittest.TestCase):
    # Small (11–12.5px) text: WCAG AA needs 4.5:1. `None` = the rule's own background.
    SMALL_TEXT = [
        ('.cms-prio[data-priority="critical"]', None), ('.cms-prio[data-priority="high"]', None),
        ('.cms-prio[data-priority="medium"]', None), ('.cms-prio[data-priority="low"]', None),
        (".cms-tag.is-warn", None), (".cms-tag.is-ok", None), (".cms-tag.is-crit", None), (".cms-tag.is-accent", None),
        (".cms-tag.is-out", "--surface"), (".cms-chip.is-remote", None), (".cms-chip", None),
        (".cms-sla.is-soon", "--surface"), (".cms-sla.is-over", "--surface"),
        (".cms-kpi-sub", "--surface"), (".cms-chart-sub", "--surface"), (".cms-chart-empty", "--surface"),
        (".cms-unverified", "--card"), (".cms-cite", "--card"), (".cms-error", None),
        # Field review: the model-worded quote, the row's message and the settled status line.
        (".cms-quote.is-model", "--surface"), (".cms-prow-msg", "--card"), (".cms-pending-done", "--surface"),
        # The detail panel: «خارج نتائج التصفية الحالية» on a panel the filters leave out.
        (".cms-d-note", None),
    ]

    def test_small_status_text_meets_wcag_aa(self):
        # F7: priority badges, «تحتاج مراجعة» tags and the KPI/chart subtitles were 2.97–4.01:1 in light mode.
        css = _read("complaints.css")
        for mode, tokens in _palettes().items():
            for selector, background in self.SMALL_TEXT:
                with self.subTest(mode=mode, selector=selector):
                    fg = _resolve(_declaration(css, selector, "color"), tokens)
                    bg = _resolve(tokens[background] if background else _declaration(css, selector, "background"), tokens)
                    self.assertGreaterEqual(round(_contrast(fg, bg), 2), 4.5, f"{fg} on {bg}")

    def test_panel_announcer_stays_in_the_accessibility_tree(self):
        # F5: `display:none` while empty drops the live region, and its next message may go unread.
        css = re.sub(r"/\*.*?\*/", "", _read("complaints.css"), flags=re.S)
        for m in re.finditer(r"([^{}]*\.cms-d-status[^{}]*)\{([^}]*)\}", css):
            with self.subTest(m.group(1).strip()):
                self.assertNotRegex(m.group(2), r"display\s*:\s*none")
                self.assertNotRegex(m.group(2), r"visibility\s*:\s*hidden")
        self.assertIn('id="cms-d-status" class="cms-d-status cms-muted" role="status"', _read("ui.html"))

    def test_no_detail_dialog_left(self):
        # The detail is an inline accordion panel now: no modal, backdrop or dialog focus handling for it.
        html = _read("ui.html")
        start = html.index('<section id="complaints-workspace"')
        workspace = html[start:html.index('<dialog id="cmp-source"', start)]    # the comparison tab's dialogs follow it
        self.assertNotIn("<dialog", workspace)
        self.assertIn('<div id="cms-detail" class="cms-panel" role="region" aria-labelledby="cms-d-title" hidden>', html)
        script = _read("complaints_ui.js")
        for needle in ("showModal", ".close()", "dialog"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, script)
        css = re.sub(r"/\*.*?\*/", "", _read("complaints.css"), flags=re.S)
        self.assertNotIn("::backdrop", css)
        self.assertNotIn("cms-dialog", css)

    def test_panel_fits_the_register_width(self):
        # The frame sticks to the start edge at the scroll box's visible width (container units, with a fallback).
        css = _read("complaints.css")
        self.assertRegex(_declaration(css, ".cms-table-wrap", "container"), r"cms-register\s*/\s*inline-size")
        frame = _block(css, "#complaints-workspace .cms-panel-frame{")
        self.assertIn("position:sticky", frame)
        self.assertIn("inset-inline-start:0", frame)
        self.assertIn("width:100cqi", frame)
        self.assertIn("@container cms-register (max-width:720px)", css)

    def test_filter_toggle_keeps_its_pill(self):
        # The row buttons once reused .cms-toggle, and their rule stripped the «تحتاج مراجعة» filter of its pill.
        css = re.sub(r"/\*.*?\*/", "", _read("complaints.css"), flags=re.S)
        self.assertEqual(len(re.findall(r"#complaints-workspace \.cms-toggle\{", css)), 1)
        self.assertIn("1px solid", _declaration(css, ".cms-toggle", "border"))
        self.assertIn('<label class="cms-toggle"><input id="cms-f-review"', _read("ui.html"))
        self.assertNotIn("'cms-toggle'", _read("complaints_ui.js"))


if __name__ == "__main__":
    unittest.main()
