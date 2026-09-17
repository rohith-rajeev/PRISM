#!/usr/bin/env python3
"""PRISM — Pull Request Inspection & Safety Manager.

Lightweight desktop UI (stdlib only) that reviews AWS CodeCommit pull
requests with a bundled reviewer agent, updates the PR description, and
auto-merges on approval. Project-agnostic: works with any CodeCommit
repository + local clone.

Run:  python3 app.py   (or ./run.sh, or the packaged desktop build)
"""
import queue
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from orchestrator import (  # noqa: E402
    list_available_models, detect_local_repos, normalise_verdict, REGION_DEFAULT,
    STAGE_REVIEW, STAGE_DESCRIBE, STAGE_MERGE_CHECK, STAGE_SYNC, STAGE_MERGE,
)
import jobs as J  # noqa: E402

# ---------- palette (dark only) ----------
PAL = {
    "page": "#1B1D22", "card": "#23262C", "border": "#373B43",
    "text": "#F0F1F3", "muted": "#9BA0A8",
    "field": "#1C1F24", "accent": "#5B9CFF",
    "accent_tint": "#1E3A5F", "accent_text": "#7FB0FF",
    "run_bg": "#F4F4F5", "run_fg": "#141414",
    "disabled_bg": "#3A3D44", "disabled_fg": "#9BA0A8",
    "log_bg": "#0F1214", "log_fg": "#C7D0D9", "log_ph": "#6E8B74",
    "good": "#34C98E", "warn": "#E5A13D", "bad": "#E5606E",
    "good_bg": "#123B2F", "bad_bg": "#3B1218",
    "badge_bg": "#1E3A5F", "badge_fg": "#7FB0FF",
}


def _mono():
    fams = set(tkfont.families())
    for name in ("Consolas", "Menlo", "DejaVu Sans Mono"):
        if name in fams:
            return (name, 10)
    return ("TkFixedFont", 10)


FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_S = ("Segoe UI", 9)
FONT_XS = ("Segoe UI", 8)
TITLE_F = ("Segoe UI", 16, "bold")

STAGE_DEFS = [
    (STAGE_REVIEW, "Review"),
    (STAGE_DESCRIBE, "Describe"),
    (STAGE_MERGE_CHECK, "Merge check"),
    (STAGE_MERGE, "Merge"),
]
STAGE_LABEL = dict(STAGE_DEFS)


def _rr(canvas, x1, y1, x2, y2, r, fill, outline, tags="rr"):
    """Rounded rectangle from arcs + rects (Tk has no native primitive)."""
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90,
                      style=tk.PIESLICE, fill=fill, outline="", tags=tags)
    canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90,
                      style=tk.PIESLICE, fill=fill, outline="", tags=tags)
    canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90,
                      style=tk.PIESLICE, fill=fill, outline="", tags=tags)
    canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90,
                      style=tk.PIESLICE, fill=fill, outline="", tags=tags)
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="", tags=tags)
    canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="", tags=tags)
    if outline:
        for a in [(x1, y1, 90), (x2 - 2 * r, y1, 0), (x1, y2 - 2 * r, 180), (x2 - 2 * r, y2 - 2 * r, 270)]:
            canvas.create_arc(a[0], a[1], a[0] + 2 * r, a[1] + 2 * r, start=a[2],
                              extent=90, style=tk.ARC, outline=outline, tags=tags)
        canvas.create_line(x1 + r, y1, x2 - r, y1, fill=outline, tags=tags)
        canvas.create_line(x1 + r, y2, x2 - r, y2, fill=outline, tags=tags)
        canvas.create_line(x1, y1 + r, x1, y2 - r, fill=outline, tags=tags)
        canvas.create_line(x2, y1 + r, x2, y2 - r, fill=outline, tags=tags)


def _spinner(canvas, cx, cy, r, frame, colour):
    """Draw one frame of a rotating arc.

    Work happens in a subprocess for minutes at a time with nothing to show
    for it, so a static dot reads as "stuck". Frames are advanced by the
    existing queue pump rather than a timer of their own — the app has
    exactly one `after` loop and keeping it that way is deliberate.
    """
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                       outline=PAL["border"], width=2)
    canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                      start=(90 - frame * 30) % 360, extent=100,
                      style=tk.ARC, outline=colour, width=2)


class RoundedCard(tk.Frame):
    """Card with rounded fill + outline. Set stretch=True when the inner
    content must expand to fill leftover space (log console)."""

    def __init__(self, parent, radius=10, fill_key="card", outline_key="border",
                 stretch=False, **kw):
        super().__init__(parent, bg=PAL["page"], **kw)
        self._r = radius
        self._fill_key = fill_key
        self._outline_key = outline_key
        self._stretch = stretch
        self.canvas = tk.Canvas(self, bg=PAL["page"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=PAL[fill_key])
        self._win = self.canvas.create_window(radius, radius, window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._cv_cfg)
        self.inner.bind("<Configure>", self._in_cfg)

    def refresh_theme(self):
        self.config(bg=PAL["page"])
        self.canvas.config(bg=PAL["page"])
        self.inner.config(bg=PAL[self._fill_key])
        self._draw()

    def _in_cfg(self, _e):
        rw = self.inner.winfo_reqwidth()
        rh = self.inner.winfo_reqheight()
        self.canvas.config(width=rw + 2 * self._r, height=rh + 2 * self._r,
                           scrollregion=(0, 0, rw + 2 * self._r, rh + 2 * self._r))
        self._draw()

    def _cv_cfg(self, e):
        try:
            cur_w = int(float(self.canvas.itemcget(self._win, "width") or 0))
        except (TypeError, ValueError):
            cur_w = 0
        want_w = max(int(e.width) - 2 * self._r, 1)
        if abs(cur_w - want_w) > 2:
            self.canvas.itemconfig(self._win, width=want_w)
        if self._stretch:
            try:
                cur_h = int(float(self.canvas.itemcget(self._win, "height") or 0))
            except (TypeError, ValueError):
                cur_h = 0
            want_h = max(int(e.height) - 2 * self._r, 1)
            if abs(cur_h - want_h) > 2:
                self.canvas.itemconfig(self._win, height=want_h)
        self._draw()

    def _draw(self):
        self.canvas.delete("rr")
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w <= 4 or h <= 4:
            return
        _rr(self.canvas, 1, 1, w - 1, h - 1, self._r,
            PAL[self._fill_key], PAL[self._outline_key])


class RoundedButton(tk.Canvas):
    """Rounded clickable button. Styles: primary | outline | ghost | field."""

    def __init__(self, parent, text="", command=None, style="outline", height=36,
                 radius=10, font=FONT_B, anchor="center", width=160):
        super().__init__(parent, height=height, width=width,
                         highlightthickness=0, bd=0)
        self._text = text
        self._command = command
        self._style = style
        self._radius = radius
        self._font = font
        self._anchor = anchor
        self._hover = False
        self._enabled = True
        self._selected = False
        self._fontobj = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: (setattr(self, "_hover", True), self._draw()))
        self.bind("<Leave>", lambda _e: (setattr(self, "_hover", False), self._draw()))
        self.bind("<Button-1>", self._click)

    def set_text(self, text):
        self._text = text
        self._draw()

    def _fit(self, text, max_px):
        """Trim `text` with an ellipsis so it cannot run past the button edge.

        Canvas text is not clipped to the widget, so an over-long clone or
        model name would otherwise be drawn straight over the rounded border.
        """
        # Ellipsising something this short only loses information — an icon
        # glyph is better clipped than replaced by "…".
        if max_px <= 0 or len(text) <= 2:
            return text
        try:
            if self._fontobj is None:
                self._fontobj = tkfont.Font(font=self._font)
            font = self._fontobj
            if font.measure(text) <= max_px:
                return text
            lo, hi = 0, len(text)
            while lo < hi:                      # longest prefix that still fits
                mid = (lo + hi + 1) // 2
                if font.measure(text[:mid] + "…") <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            return text[:lo].rstrip() + "…"
        except Exception:  # noqa: BLE001 - never fail a redraw over text metrics
            return text

    def set_enabled(self, enabled):
        self._enabled = enabled
        self._draw()

    def set_selected(self, selected):
        """Toggle state for buttons used as a segmented choice (BE / FE)."""
        self._selected = bool(selected)
        self._draw()

    def refresh_theme(self):
        self.config(bg=self.master.cget("bg") if self._style == "field" else PAL["page"])
        self._draw()

    def _click(self, _e):
        if self._enabled and self._command:
            self._command()

    def _colors(self):
        s = self._style
        if not self._enabled:
            return PAL["disabled_bg"], "", PAL["disabled_fg"]
        if self._selected:
            return PAL["accent"], PAL["accent"], "white"
        if s == "primary":
            fill = PAL["run_bg"]
            if self._hover:
                fill = PAL["accent"] if PAL["run_bg"].lower().startswith("#f") else "#2E2E2E"
            return fill, "", PAL["run_fg"]
        if s == "ghost":
            return PAL["card"], "", (PAL["accent"] if self._hover else PAL["muted"])
        if s == "field":
            return PAL["field"], PAL["border"], PAL["text"]
        # outline
        if self._hover:
            return PAL["accent_tint"], PAL["accent"], PAL["text"]
        return PAL["card"], PAL["border"], PAL["text"]

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 4 or h <= 4:
            return
        fill, outline, fg = self._colors()
        page = self.master.cget("bg")
        self.config(bg=page)
        _rr(self, 1, 1, w - 1, h - 1, min(self._radius, h // 2 - 1), fill, outline or None)
        left_pad = 14
        x = left_pad if self._anchor == "w" else w // 2
        # Available text width differs by alignment: left-aligned text starts
        # at left_pad and needs a small right margin, centred text just needs a
        # margin either side. Using the left-aligned budget for both starved
        # narrow icon buttons (the 36px "↻") into rendering as a bare ellipsis.
        avail = (w - left_pad - 10) if self._anchor == "w" else (w - 20)
        label = self._fit(self._text, avail)
        self.create_text(x, h // 2, text=label, fill=fg, font=self._font,
                         anchor="w" if self._anchor == "w" else "center")


class ProgressBar(tk.Canvas):
    """Pipeline progress: one continuous track plus stage captions.

    Deliberately not a segmented control. The previous version drew four
    bordered cells with dividers, which reads as a row of clickable tabs —
    people tried to click it. Here there are no cell borders and no dividers;
    a single fill grows left to right, which is the one shape everybody
    already understands as progress.
    """

    TRACK_TOP, TRACK_H = 6, 7

    def __init__(self, parent, height=44):
        super().__init__(parent, height=height, highlightthickness=0, bd=0)
        self.reset()
        self.bind("<Configure>", lambda _e: self._draw())

    def reset(self):
        self.states = {sid: "pending" for sid, _ in STAGE_DEFS}
        self.notes = {}
        self._draw()

    def set_stage(self, sid, state, text=None):
        if sid not in self.states:
            return
        self.states[sid] = state
        if text is not None:
            self.notes[sid] = text
        elif state != "active":
            self.notes.pop(sid, None)
        self._draw()

    def refresh_theme(self):
        self.config(bg=PAL["page"])
        self._draw()

    def _fraction(self):
        """How full the track is: resolved stages, plus half for one in flight."""
        n = len(STAGE_DEFS)
        states = [self.states.get(sid, "pending") for sid, _ in STAGE_DEFS]
        if all(s in ("done", "skipped") for s in states):
            return 1.0
        for i, s in enumerate(states):
            if s in ("active", "error"):
                return (i + 0.5) / n        # stop on the current stage's dot
        done = sum(1 for s in states if s in ("done", "skipped"))
        return done / n

    def _fill_colour(self):
        if any(s == "error" for s in self.states.values()):
            return PAL["bad"]
        if all(self.states.get(sid) in ("done", "skipped") for sid, _ in STAGE_DEFS):
            return PAL["good"]
        return PAL["accent"]

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 10 or h <= 10:
            return
        self.config(bg=PAL["page"])
        n = len(STAGE_DEFS)
        y0, y1 = self.TRACK_TOP, self.TRACK_TOP + self.TRACK_H
        r = self.TRACK_H / 2

        _rr(self, 0, y0, w, y1, r, PAL["field"], None)          # track
        frac = self._fraction()
        if frac > 0:
            _rr(self, 0, y0, max(self.TRACK_H, w * frac), y1, r, self._fill_colour(), None)

        for i, (sid, label) in enumerate(STAGE_DEFS):
            state = self.states.get(sid, "pending")
            cx = w * (i + 0.5) / n
            mx = min(max(cx, r + 1), w - r - 1)   # clamped so end dots stay on the track
            if state == "done":
                dot, fg, font = PAL["good"], PAL["good"], FONT_XS
            elif state == "active":
                dot, fg, font = PAL["accent_text"], PAL["accent_text"], ("Segoe UI", 8, "bold")
            elif state == "error":
                dot, fg, font = PAL["bad"], PAL["bad"], ("Segoe UI", 8, "bold")
            elif state == "skipped":
                dot, fg, font = PAL["border"], PAL["muted"], FONT_XS
            else:
                dot, fg, font = PAL["border"], PAL["muted"], FONT_XS
            self.create_oval(mx - 3, (y0 + y1) / 2 - 3, mx + 3, (y0 + y1) / 2 + 3,
                             fill=dot, outline=PAL["page"], width=2)
            mark = {"done": "✓ ", "error": "✕ ", "skipped": "– "}.get(state, "")
            caption = self.notes.get(sid) or label
            self.create_text(cx, y1 + 13, text=f"{mark}{caption}", fill=fg,
                             font=font, anchor="n")


class StatusPill(tk.Canvas):
    """Outlined status pill (Idle / Running / Merged / …)."""

    def __init__(self, parent, width=150, height=28):
        super().__init__(parent, width=width, height=height, highlightthickness=0, bd=0)
        self._text = "Idle"
        self._color = PAL["muted"]
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, text, color_key="muted", spin=False):
        self._text = text
        self._color_key = color_key
        self._color = PAL[color_key]
        self._spin = spin
        self._draw()

    def tick(self, frame):
        """Advance the animation. No-op unless this pill is spinning."""
        if getattr(self, "_spin", False):
            self._frame = frame
            self._draw()

    def refresh_theme(self):
        self.config(bg=PAL["page"])
        self._color = PAL.get(getattr(self, "_color_key", "muted"), PAL["muted"])
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 150
        h = self.winfo_height() or 28
        self.config(bg=PAL["page"])
        _rr(self, 1, 1, w - 1, h - 1, (h - 2) // 2, PAL["card"], PAL["border"])
        if getattr(self, "_spin", False):
            _spinner(self, 18, h / 2, 5, getattr(self, "_frame", 0), self._color)
        else:
            self.create_oval(14, h / 2 - 4, 22, h / 2 + 4, fill=self._color, outline="")
        self.create_text(30, h / 2, text=self._text, fill=PAL["text"], font=FONT_S, anchor="w")


class CheckRow(tk.Frame):
    """Rounded-square custom checkbox + label."""

    def __init__(self, parent, text, var, muted=False):
        super().__init__(parent, bg=PAL["card"])
        self.var = var
        self._muted = muted
        self.box = tk.Canvas(self, width=20, height=20, highlightthickness=0, bd=0)
        self.box.pack(side="left")
        self.label = tk.Label(self, text=text, font=FONT_S,
                              bg=PAL["card"], fg=PAL["muted"] if muted else PAL["text"])
        self.label.pack(side="left", padx=(8, 0))
        self.box.bind("<Button-1>", lambda _e: self.toggle())
        self.label.bind("<Button-1>", lambda _e: self.toggle())
        self.refresh_theme()

    def toggle(self):
        self.var.set(not self.var.get())
        self._draw()

    def refresh_theme(self):
        self.config(bg=PAL["card"])
        self.label.config(bg=PAL["card"],
                          fg=PAL["muted"] if self._muted else PAL["text"])
        self.box.config(bg=PAL["card"])
        self._draw()

    def _draw(self):
        self.box.delete("all")
        on = bool(self.var.get())
        fill = PAL["accent"] if on else PAL["card"]
        _rr(self.box, 2, 2, 18, 18, 5, fill, PAL["accent"] if on else PAL["muted"])
        if on:
            self.box.create_text(10, 10, text="✓", fill="white", font=("Segoe UI", 9, "bold"))


class AgentPanel(RoundedCard):
    """Conditional conversation panel — hidden until the reviewer asks.

    The agent stops and asks when an input is missing or a branch is
    ambiguous; without this the run simply dead-ended on "could not parse a
    verdict". Height is deliberately fixed: the panel appears mid-run, above
    the log, and a panel that grew with the question pushed its own Send
    button off-screen. The question scrolls instead. Plain Tk — no new
    dependencies, nothing added to the packaged size.
    """

    def __init__(self, parent, on_send, on_skip):
        super().__init__(parent, fill_key="card", outline_key="accent")
        self._on_send, self._on_skip = on_send, on_skip
        inner = self.inner
        inner.config(padx=12, pady=7)

        # Actions live on the header row rather than a row of their own: it
        # keeps them at a fixed, always-visible position and saves the height.
        head = tk.Frame(inner, bg=PAL["card"])
        head.pack(fill="x")
        lb = tk.Label(head, text="💬  Reviewer needs your input", font=FONT_B,
                      bg=PAL["card"], fg=PAL["accent_text"])
        lb.pack(side="left")
        RoundedButton(head, text="Send  ▸", command=self._send, style="primary",
                      height=26, width=104, font=FONT_S).pack(side="right")
        RoundedButton(head, text="Skip", command=self._skip, style="outline",
                      height=26, width=74, font=FONT_S).pack(side="right", padx=(0, 6))

        self.question = tk.Text(inner, height=3, font=FONT_S, bg=PAL["card"],
                                fg=PAL["text"], relief="flat", wrap="word",
                                highlightthickness=0, cursor="arrow")
        self.question.pack(fill="x", pady=(5, 5))
        self.question.config(state="disabled")

        self.entry = tk.Text(inner, height=2, font=FONT, bg=PAL["field"],
                             fg=PAL["text"], insertbackground=PAL["text"],
                             relief="flat", wrap="word", highlightthickness=1,
                             highlightbackground=PAL["border"],
                             highlightcolor=PAL["accent"])
        self.entry.pack(fill="x")
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Shift-Return>", lambda _e: None)
        hint = tk.Label(inner, text="Enter to send · Shift+Enter for a new line",
                        font=FONT_XS, bg=PAL["card"], fg=PAL["muted"], anchor="w")
        hint.pack(fill="x", pady=(3, 0))

    def present(self, question):
        self.question.config(state="normal")
        self.question.delete("1.0", "end")
        self.question.insert("1.0", question)
        self.question.config(state="disabled")
        self.question.see("1.0")
        self.entry.delete("1.0", "end")
        self.entry.focus_set()

    def _text(self):
        return self.entry.get("1.0", "end").strip()

    def _enter(self, _e):
        self._send()
        return "break"          # keep Tk from also inserting the newline

    def _send(self):
        msg = self._text()
        if msg:
            self._on_send(msg)

    def _skip(self):
        self._on_skip()


class Dialog(tk.Toplevel):
    """Modal in PRISM's own palette.

    Tk's messagebox draws a light system panel, which is jarring against a
    dark app and — for the quit prompt especially — makes the one dialog that
    destroys work look like it belongs to something else. This is the same
    rounded card, button and colour language as the rest of the UI.
    """

    def __init__(self, parent, title, message, confirm=None, cancel="OK",
                 tone="warn"):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(bg=PAL["page"])
        self.resizable(False, False)
        self.transient(parent)
        self.result = False

        card = RoundedCard(self, outline_key=tone)
        card.pack(fill="both", expand=True, padx=10, pady=10)
        inner = card.inner
        inner.config(padx=16, pady=12)

        head = tk.Frame(inner, bg=PAL["card"])
        head.pack(fill="x", pady=(0, 8))
        glyph = {"bad": "⛔", "warn": "⚠", "accent": "💬"}.get(tone, "⚠")
        tk.Label(head, text=glyph, font=("Segoe UI", 14), bg=PAL["card"],
                 fg=PAL[tone]).pack(side="left", padx=(0, 10))
        tk.Label(head, text=title, font=("Segoe UI", 12, "bold"), bg=PAL["card"],
                 fg=PAL["text"], anchor="w").pack(side="left")

        tk.Label(inner, text=message, font=FONT_S, bg=PAL["card"], fg=PAL["text"],
                 justify="left", anchor="w", wraplength=430).pack(fill="x")

        row = tk.Frame(inner, bg=PAL["card"])
        row.pack(fill="x", pady=(14, 0))
        # Cancel sits on the right and takes focus: the destructive choice
        # should never be the one a stray Enter or Space triggers.
        cancel_btn = RoundedButton(row, text=cancel, height=32, width=120,
                                   font=FONT_B, command=self._cancel)
        cancel_btn.pack(side="right")
        if confirm:
            RoundedButton(row, text=confirm, height=32, width=140, font=FONT_B,
                          style="primary",
                          command=self._confirm).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.update_idletasks()
        self._centre(parent)
        self.deiconify()
        cancel_btn.focus_set()
        self.grab_set()
        self.wait_window(self)

    def _centre(self, parent):
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 3
        except Exception:  # noqa: BLE001
            x = y = 200
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _confirm(self):
        self.result = True
        self.destroy()

    def _cancel(self):
        self.result = False
        self.destroy()


def ask_confirm(parent, title, message, confirm="Continue", cancel="Cancel",
                tone="warn"):
    """Themed yes/no. Returns True only on the explicit confirm button."""
    return Dialog(parent, title, message, confirm=confirm, cancel=cancel,
                  tone=tone).result


def show_warning(parent, title, message):
    """Themed acknowledgement, replacing messagebox.showwarning."""
    Dialog(parent, title, message, confirm=None, cancel="OK", tone="warn")


class ScrollFrame(tk.Frame):
    """Scrollable container in pure Tk (canvas + inner frame + scrollbar).

    Extracted from Picker.open(), which has used this shape for its popup all
    along. One difference: a popup binds the wheel to its transient Toplevel,
    whereas an embedded list must bind to its own canvas or it would steal
    wheel events from the whole window.
    """

    def __init__(self, parent, bg_key="page"):
        super().__init__(parent, bg=PAL[bg_key])
        self._bg = bg_key
        self.canvas = tk.Canvas(self, bg=PAL[bg_key], highlightthickness=0, bd=0)
        self.scroll = tk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=PAL[bg_key])
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfig(self._win, width=e.width))
        self.inner.bind("<Configure>", self._resized)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            for w in (self.canvas, self.inner):
                w.bind(seq, self._wheel)

    def _resized(self, _e):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        # Only show the scrollbar when there is something to scroll.
        need = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        if need and not self.scroll.winfo_manager():
            self.scroll.pack(side="right", fill="y")
        elif not need and self.scroll.winfo_manager():
            self.scroll.pack_forget()

    def _wheel(self, e):
        step = -1 if (getattr(e, "delta", 0) > 0 or e.num == 4) else 1
        self.canvas.yview_scroll(step, "units")


class JobRow(RoundedCard):
    """One line in the jobs list: status, target, verdict, impact, actions."""

    def __init__(self, parent, job, on_open, on_stop, on_remove):
        super().__init__(parent)
        self.job = job
        inner = self.inner
        inner.config(padx=12, pady=7)
        inner.columnconfigure(1, weight=1)

        self.dot = tk.Canvas(inner, width=14, height=14, highlightthickness=0,
                             bd=0, bg=PAL["card"])
        self.dot.grid(row=0, column=0, rowspan=2, padx=(0, 10))

        self.title = tk.Label(inner, text=job.label, font=FONT_B, bg=PAL["card"],
                              fg=PAL["text"], anchor="w", cursor="hand2")
        self.title.grid(row=0, column=1, sticky="w")
        self.sub = tk.Label(inner, text="", font=FONT_XS, bg=PAL["card"],
                            fg=PAL["muted"], anchor="w", cursor="hand2")
        self.sub.grid(row=1, column=1, sticky="w")

        self.verdict = tk.Label(inner, text="—", font=FONT_S, bg=PAL["card"],
                                fg=PAL["muted"], anchor="w", width=24)
        self.verdict.grid(row=0, column=2, rowspan=2, sticky="w", padx=(10, 6))
        self.impact = tk.Label(inner, text="", font=FONT_S, bg=PAL["card"],
                               fg=PAL["muted"], anchor="w", width=9)
        self.impact.grid(row=0, column=3, rowspan=2, sticky="w", padx=(0, 8))

        self.stop_btn = RoundedButton(inner, text="Stop", style="outline",
                                      height=26, width=66, font=FONT_XS,
                                      command=lambda: on_stop(job.id))
        self.stop_btn.grid(row=0, column=4, rowspan=2, padx=(0, 6))
        self.del_btn = RoundedButton(inner, text="✕", style="ghost", height=26,
                                     width=30, font=FONT_XS,
                                     command=lambda: on_remove(job.id))
        self.del_btn.grid(row=0, column=5, rowspan=2)

        for w in (inner, self.title, self.sub):
            w.bind("<Button-1>", lambda _e: on_open(job.id))
        self.refresh()

    def tick(self, frame):
        if getattr(self, "_spin", False):
            self._frame = frame
            self.refresh()

    def refresh(self):
        job = self.job
        colour = {J.RUNNING: "accent", J.NEEDS_INPUT: "warn", J.QUEUED: "muted",
                  J.STOPPING: "warn", J.STOPPED: "muted", J.ERROR: "bad",
                  J.DONE: "good"}.get(job.status, "muted")
        self.dot.delete("all")
        self.dot.config(bg=PAL["card"])
        # Spin only while genuinely working — a job waiting on the user is not
        # making progress and should not pretend to be.
        self._spin = job.status in (J.RUNNING, J.STOPPING)
        if self._spin:
            _spinner(self.dot, 7, 7, 5, getattr(self, "_frame", 0), PAL[colour])
        else:
            self.dot.create_oval(3, 3, 12, 12, fill=PAL[colour], outline="")
        label = {J.NEEDS_INPUT: "Needs input", J.QUEUED: "Queued",
                 J.RUNNING: "Running…", J.STOPPING: "Stopping…",
                 J.STOPPED: "Stopped", J.ERROR: "Error",
                 J.DONE: "Finished"}.get(job.status, job.status)
        if job.status == J.DONE and (job.result or {}).get("merged") is True:
            label = "Merged ✓"
        self.sub.config(text=f"{label}  ·  {job.spec.summary_bits()}")
        if job.verdict_raw:
            self.verdict.config(text=job.verdict_raw[:26],
                                fg=PAL[_verdict_colour(job.verdict_key)])
        # Same treatment as the detail card: score plus a severity icon. The
        # agent's reason is a free-text clause and would be truncated to noise.
        m = _IMPACT_RE.search(job.impact or "")
        if m:
            icon, colour = _impact_style(m.group(1), m.group(2))
            self.impact.config(text=f"{icon} {m.group(1)}/{m.group(2)}".strip(),
                               fg=PAL[colour])
        else:
            self.impact.config(text="", fg=PAL["muted"])
        self.stop_btn.set_enabled(job.is_active)
        self.del_btn.set_enabled(True)


class Picker(tk.Frame):
    """Fixed-style selector: rounded field + searchable popup.

    Items can be grouped under headers via `group_key`; a falsy key renders
    a flat list. Stdlib tkinter only.
    """

    def __init__(self, parent, title="SELECT", empty_label="Select…",
                 field_chars=28, group_key=None, display_fn=None, empty_hint="",
                 field_height=30, field_width=None, on_change=None):
        super().__init__(parent, bg=PAL["card"])
        self._title = title
        self._empty_label = empty_label
        self._empty_hint = empty_hint
        self._display_fn = display_fn or (lambda v: v)
        self._on_change = on_change
        self._group_key = group_key or (lambda m: (m.partition("/")[0] or "other").upper())
        self._models: list = []
        self._value = ""
        self._popup = None
        self._outside_funcid = None
        self.field = RoundedButton(self, text=empty_label + "  ▾", command=self.toggle,
                                   style="field", height=field_height,
                                   font=FONT, anchor="w",
                                   width=field_width or field_chars * 8)
        self.field.pack(fill="x", expand=True)

    def refresh_theme(self):
        self.config(bg=PAL["card"])
        self._refresh_field()

    def get(self):
        return self._value

    def set_models(self, models):
        self._models = sorted(set(models or []))
        if self._value and self._value not in self._models:
            self._value = ""
        self._refresh_field()

    def set_custom(self, value):
        """Set a value that isn't in the list (e.g. a browsed folder path)."""
        self._value = value or ""
        self._refresh_field()

    def _refresh_field(self):
        label = self._display_fn(self._value) if self._value else self._empty_label
        self.field.set_text(f"{label}  ▾")

    # ----- popup -----
    def toggle(self):
        if self._popup is not None:
            self.close()
        else:
            self.open()

    def open(self):
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.configure(bg=PAL["border"])
        x = self.field.winfo_rootx()
        y = self.field.winfo_rooty() + self.field.winfo_height() + 4
        w = max(self.field.winfo_width(), 340)
        top.geometry(f"{w}x340+{x}+{y}")
        top.attributes("-topmost", True)

        head = tk.Frame(top, bg=PAL["border"])
        head.pack(fill="x")
        tk.Label(head, text=f"  {self._title}", font=("Segoe UI", 8, "bold"),
                 bg=PAL["border"], fg=PAL["muted"]).pack(side="left", pady=4)
        tk.Button(head, text="✕", command=self.close, bg=PAL["border"], fg=PAL["muted"],
                  activebackground=PAL["bad"], activeforeground="white", relief="flat",
                  font=FONT_S, cursor="hand2").pack(side="right", padx=2)

        self._search_var = tk.StringVar()
        search = tk.Entry(top, textvariable=self._search_var, font=FONT_S,
                          bg=PAL["card"], fg=PAL["text"], insertbackground=PAL["text"],
                          relief="flat")
        search.pack(fill="x", padx=6, pady=6, ipady=5)

        body = tk.Frame(top, bg=PAL["card"])
        body.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        canvas = tk.Canvas(body, bg=PAL["card"], highlightthickness=0)
        scroll = tk.Scrollbar(body, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=PAL["card"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            top.bind(seq, lambda e, s=seq: canvas.yview_scroll(
                -1 if (s == "<MouseWheel>" and e.delta > 0) or s == "<Button-4>" else 1, "units"))

        self._popup = top
        self._inner = inner
        self._search_var.trace_add("write", lambda *_a: self._render())
        top.bind("<Escape>", lambda _e: self.close())
        search.bind("<Escape>", lambda _e: self.close())
        self._render()
        search.focus_set()
        root = self.winfo_toplevel()
        self._outside_funcid = root.bind("<Button-1>", self._outside_click, add="+")

    def _render(self):
        if self._popup is None:
            return
        for w in self._inner.winfo_children():
            w.destroy()
        if not self._models and self._empty_hint and not self._search_var.get().strip():
            tk.Label(self._inner, text=self._empty_hint, font=FONT_S,
                     bg=PAL["card"], fg=PAL["muted"]).pack(anchor="w", padx=12, pady=12)
            return
        filt = self._search_var.get().strip().lower()
        groups: dict = {}
        for m in self._models:
            if filt and filt not in m.lower():
                continue
            key = self._group_key(m) or ""
            if key and "/" in m:
                short = m.split("/", 1)[1]
            else:
                short = m
            groups.setdefault(key, []).append((short, m))
        if not groups:
            tk.Label(self._inner, text="No matches", font=FONT_S,
                     bg=PAL["card"], fg=PAL["muted"]).pack(anchor="w", padx=12, pady=12)
            return
        for key in sorted(groups):
            if key:
                tk.Label(self._inner, text=f"  {key}", font=("Segoe UI", 8, "bold"),
                         bg=PAL["card"], fg=PAL["muted"]).pack(anchor="w", padx=4, pady=(8, 0))
            for name, full in sorted(groups[key]):
                sel = full == self._value
                b = tk.Label(self._inner, text=f"{'● ' if sel else '○ '}{name}",
                             font=FONT_S, bg=PAL["accent_tint"] if sel else PAL["card"],
                             fg=PAL["accent_text"] if sel else PAL["text"], anchor="w",
                             padx=18, pady=3, cursor="hand2")
                b.pack(fill="x")
                b.bind("<Button-1>", lambda _e, f=full: self.select(f))
                b.bind("<Enter>", lambda e: e.widget.config(bg=PAL["accent_tint"]))
                b.bind("<Leave>", lambda e, f=full: e.widget.config(
                    bg=PAL["accent_tint"] if f == self._value else PAL["card"]))

    def select(self, full):
        self._value = full
        self._refresh_field()
        self.close()
        if self._on_change:
            try:
                self._on_change(full)
            except Exception:  # noqa: BLE001
                pass

    def _outside_click(self, event):
        if self._popup is None:
            return
        if event.widget is self.field:
            return  # toggle() already handled the field click (widget bindings run first)
        self.close()

    def close(self):
        if self._outside_funcid is not None:
            try:
                self.winfo_toplevel().unbind("<Button-1>", self._outside_funcid)
            except Exception:  # noqa: BLE001
                pass
            self._outside_funcid = None
        if self._popup is not None:
            try:
                self._popup.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._popup = None


class App(tk.Tk):
    """Controller. Owns the jobs, the queue pump, and three sibling screens.

    Only one set of run-output widgets exists: the detail screen renders
    whichever job is selected rather than there being one screen per job.
    """

    def __init__(self):
        super().__init__(className="prism")
        self.title("PRISM")
        self.geometry("1020x900")
        self.minsize(900, 620)
        self.configure(bg=PAL["page"])
        self.log_q = queue.Queue()
        self.manager = J.JobManager(self.log_q)
        self.selected_job_id = None
        self.last_spec = None        # seeds the next new-job form
        self.rows = {}               # job id -> JobRow
        self.screen = None
        self.stage_state = {}        # mirrors the displayed job, for the bar
        self._anim = 0               # spinner frame, advanced by the pump
        self._verdict_key = ""
        self._drain_job = None
        self._build()
        self._drain_job = self.after(80, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- themed primitives -----
    def _lab(self, parent, text, font=FONT_XS, fg="muted", bg="card"):
        lb = tk.Label(parent, text=text, font=font, bg=PAL[bg], fg=PAL[fg])
        lb._roles = {"bg": bg, "fg": fg}  # noqa: SLF001
        return lb

    def _entry(self, parent, var=None, font=FONT, width=None, mono=False):
        e = tk.Entry(parent, textvariable=var, font=_mono() if mono else font,
                     bg=PAL["field"], fg=PAL["text"], insertbackground=PAL["text"],
                     relief="flat", highlightthickness=1,
                     highlightbackground=PAL["border"], highlightcolor=PAL["accent"])
        if width:
            e.config(width=width)
        e._roles = {"bg": "field", "fg": "text",  # noqa: SLF001
                    "insertbackground": "text",
                    "highlightbackground": "border", "highlightcolor": "accent"}
        return e

    # ----- layout -----
    def _build(self):
        root = tk.Frame(self, bg=PAL["page"])
        root.pack(fill="both", expand=True, padx=14, pady=8)

        # ---- header: identity, live job tally, and the back affordance ----
        h = tk.Frame(root, bg=PAL["page"])
        h.pack(fill="x", pady=(0, 6))
        self._badge = tk.Canvas(h, width=32, height=32, highlightthickness=0, bd=0)
        self._badge.pack(side="left")
        titles = tk.Frame(h, bg=PAL["page"])
        titles.pack(side="left", padx=(12, 0))
        self._title_lb = tk.Label(titles, text="PRISM", font=TITLE_F,
                                  bg=PAL["page"], fg=PAL["text"])
        self._title_lb.pack(anchor="w")
        self._sub_lb = tk.Label(titles, text="Pull request inspection and safety manager",
                                font=FONT_S, bg=PAL["page"], fg=PAL["muted"])
        self._sub_lb.pack(anchor="w")
        self.pill = StatusPill(h)
        self.pill.pack(side="right")
        self.pill.set("Idle", "muted")
        self.back_btn = RoundedButton(h, text="←  Jobs", style="outline", height=30,
                                      width=100, font=FONT_S, command=self.show_jobs)
        # packed/unpacked by the router

        # ---- one container, three sibling screens, one mapped at a time ----
        self.container = tk.Frame(root, bg=PAL["page"])
        self.container.pack(fill="both", expand=True)

        self.jobs_screen = tk.Frame(self.container, bg=PAL["page"])
        self.new_screen = tk.Frame(self.container, bg=PAL["page"])
        self.detail_screen = tk.Frame(self.container, bg=PAL["page"])

        self._build_jobs_screen(self.jobs_screen)
        self._build_new_screen(self.new_screen)
        self._build_detail_screen(self.detail_screen)

        self._draw_badge()
        self._refresh_detection()
        self._detect_job = None
        self.proj_var.trace_add("write", self._on_proj_changed)
        self._load_models_async()
        self.show_jobs()

    # ---------------- screen 1: the jobs list ----------------
    def _build_jobs_screen(self, parent):
        head = tk.Frame(parent, bg=PAL["page"])
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="JOBS", font=FONT_XS, bg=PAL["page"],
                 fg=PAL["muted"]).pack(side="left")
        RoundedButton(head, text="+  New job", command=self.show_new,
                      style="primary", height=32, width=130,
                      font=FONT_B).pack(side="right")
        self.jobs_list = ScrollFrame(parent)
        self.jobs_list.pack(fill="both", expand=True)
        self.jobs_empty = tk.Label(
            self.jobs_list.inner,
            text="No jobs yet.\n\nStart one with “New job” — each pull request "
                 "runs as its own job,\nand several can run at the same time.",
            font=FONT_S, bg=PAL["page"], fg=PAL["muted"], justify="center")

    # ---------------- screen 2: set up a new job ----------------
    def _build_new_screen(self, parent):
        root = parent
        # ---- Project card ----
        pc = RoundedCard(root)
        pc.pack(fill="x", pady=(0, 6))
        pi = pc.inner
        pi.config(padx=12, pady=5)
        self._lab(pi, "Project", font=FONT_B, fg="text").pack(anchor="w", pady=(0, 2))
        self._lab(pi, "Working folder").pack(anchor="w", pady=(0, 4))
        frow = tk.Frame(pi, bg=PAL["card"])
        frow.pack(fill="x", pady=(0, 6))
        self.proj_var = tk.StringVar(value=_default_project_dir())
        self._entry(frow, self.proj_var, mono=True).pack(side="left", fill="x",
                                                         expand=True, ipady=3, padx=(0, 10))
        RoundedButton(frow, text="Browse", command=self._browse, style="outline",
                      height=32, width=110).pack(side="left")
        trow = tk.Frame(pi, bg=PAL["card"])
        trow.pack(fill="x")
        # Left cell is rebuilt per mode: single-repo → CodeCommit repo textbox;
        # multi-repo → Backend/Frontend target toggle. PR id + Region stay put.
        self.leftbox = tk.Frame(trow, bg=PAL["card"])
        self.leftbox.pack(side="left", fill="x", expand=True, padx=(0, 10))
        c2 = tk.Frame(trow, bg=PAL["card"])
        c2.pack(side="left", padx=(0, 10))
        self._lab(c2, "PR id").pack(anchor="w", pady=(0, 4))
        self.pr_var = tk.StringVar()
        self.pr_entry = self._entry(c2, self.pr_var, width=12)
        self.pr_entry.pack(ipady=3)
        _add_placeholder(self.pr_entry, "e.g. 214")
        c3 = tk.Frame(trow, bg=PAL["card"])
        c3.pack(side="left")
        self._lab(c3, "Region").pack(anchor="w", pady=(0, 4))
        self.region_var = tk.StringVar(value=REGION_DEFAULT)
        self._entry(c3, self.region_var, width=14).pack(ipady=3)
        self.clone_hint = self._lab(pi, "", font=FONT_XS, fg="muted")
        self.clone_hint.pack(anchor="w", pady=(4, 0))
        # detection state — filled by _refresh_detection()
        self.repo_var = tk.StringVar()
        self.target_var = tk.StringVar(value="BE")
        self.mode = "single"
        self.detected = []      # [(name, path)] — subdir clones, never hardcoded
        self.single_path = None  # resolved single clone path (None → API mode)
        self.tbe_btn = None
        self.tfe_btn = None
        self._rebuild_target_row()

        # ---- middle row: mapping (left) and model/behavior (right) ----
        # Two half-width cards instead of two stacked full-width ones. The
        # mapping card only exists for multi-repo projects, so when it is
        # hidden the model card takes the whole row rather than leaving a gap.
        self.midrow = tk.Frame(root, bg=PAL["page"])
        self.midrow.pack(fill="x", pady=(0, 6))
        self.midrow.columnconfigure(0, weight=1, uniform="mid")
        self.midrow.columnconfigure(1, weight=1, uniform="mid")

        self.map_card = RoundedCard(self.midrow)
        mapi = self.map_card.inner
        mapi.config(padx=12, pady=6)
        self._lab(mapi, "Repository mapping", font=FONT_B, fg="text").pack(anchor="w")
        self._lab(mapi, "Which clone is backend / frontend.").pack(anchor="w", pady=(0, 6))

        def _clone_row(label, title, empty_label, pady):
            col = tk.Frame(mapi, bg=PAL["card"])
            col.pack(fill="x", pady=pady)
            lb = tk.Label(col, text=label, font=FONT_XS, bg=PAL["card"],
                          fg=PAL["muted"], anchor="w")
            lb.pack(anchor="w", pady=(0, 2))
            picker = Picker(col, title=title, empty_label=empty_label,
                            group_key=lambda m: "", empty_hint="No clones detected")
            picker.pack(fill="x")
            return picker

        self.be_picker = _clone_row("Backend", "SELECT BACKEND CLONE",
                                    "Select backend clone…", (0, 0))
        self.fe_picker = _clone_row("Frontend", "SELECT FRONTEND CLONE",
                                    "Select frontend clone…", (6, 0))

        mc = RoundedCard(self.midrow)
        self.model_card = mc
        mi = mc.inner
        mi.config(padx=12, pady=6)
        mhead = tk.Frame(mi, bg=PAL["card"])
        mhead.pack(fill="x")
        self._lab(mhead, "Model and behavior", font=FONT_B, fg="text").pack(side="left")
        self.model_refresh = RoundedButton(mhead, text="↻", command=self._load_models_async,
                                           style="ghost", height=22, radius=11,
                                           font=FONT_S, width=30)
        self.model_refresh.pack(side="right")
        self.model_count = self._lab(mhead, "loading…", font=FONT_XS, fg="muted")
        self.model_count.pack(side="right", padx=(0, 8))
        self.model_picker = Picker(mi, title="SELECT MODEL", empty_label="Default model",
                                   group_key=None, empty_hint="Loading models…")
        self.model_picker.pack(fill="x", pady=(6, 4))
        self.upd_var = tk.BooleanVar(value=True)
        self.mrg_var = tk.BooleanVar(value=True)
        self.syn_var = tk.BooleanVar(value=True)
        self.dry_var = tk.BooleanVar(value=False)
        for txt, var, muted in (("Update PR description after review", self.upd_var, False),
                                ("Auto-merge once approved", self.mrg_var, False),
                                ("Sync with base branch when diverged", self.syn_var, False),
                                ("Dry run — skip writes and merges", self.dry_var, True)):
            CheckRow(mi, txt, var, muted=muted).pack(anchor="w")
        self._place_midrow()


        arow = tk.Frame(root, bg=PAL["page"])
        arow.pack(fill="x", pady=(2, 0))
        self.run_btn = RoundedButton(arow, text="▶  Start Prisming", command=self._start,
                                     style="primary", height=38, radius=10,
                                     font=("Segoe UI", 11, "bold"))
        self.run_btn.pack(side="left", fill="x", expand=True)
        RoundedButton(arow, text="Cancel", command=self.show_jobs, style="outline",
                      height=38, radius=10, font=FONT_B,
                      width=120).pack(side="left", padx=(8, 0))

    # ---------------- screen 3: one job in detail ----------------
    def _build_detail_screen(self, parent):
        root = parent
        top = RoundedCard(root)
        top.pack(fill="x", pady=(0, 6))
        ti = top.inner
        ti.config(padx=12, pady=7)
        self.detail_title = tk.Label(ti, text="", font=FONT_B, bg=PAL["card"],
                                     fg=PAL["text"], anchor="w")
        self.detail_title.pack(side="left")
        self.stop_btn = RoundedButton(ti, text="■  Stop", command=self._stop,
                                      style="outline", height=30, width=110,
                                      font=FONT_S)
        self.stop_btn.pack(side="right")
        self.stop_btn.set_enabled(False)
        # ---- progress: below the action, because it reports on it ----
        self.seg = ProgressBar(root)
        self.seg.pack(fill="x", pady=(2, 4))

        # ---- verdict + impact, one compact row ----
        vc = RoundedCard(root)
        vc.pack(fill="x", pady=(0, 6))
        vi = vc.inner
        vi.config(padx=14, pady=4)
        vi.columnconfigure(0, weight=3, uniform="v")
        vi.columnconfigure(1, weight=2, uniform="v")
        self._lab(vi, "VERDICT").grid(row=0, column=0, sticky="w")
        self._lab(vi, "IMPACT").grid(row=0, column=1, sticky="w")
        self.verdict_val = tk.Label(vi, text="Not run yet", font=("Segoe UI", 11, "bold"),
                                    bg=PAL["card"], fg=PAL["text"], anchor="w",
                                    justify="left")
        self.verdict_val.grid(row=1, column=0, sticky="ew")
        self.impact_val = tk.Label(vi, text="—", font=("Segoe UI", 11, "bold"),
                                   bg=PAL["card"], fg=PAL["text"], anchor="w",
                                   justify="left")
        self.impact_val.grid(row=1, column=1, sticky="ew")

        # ---- agent conversation (packed only while a question is open) ----
        self.agent_panel = AgentPanel(root, on_send=self._answer_agent,
                                      on_skip=lambda: self._answer_agent(""))

        # ---- log console (expands; squeezes first on short windows) ----
        lc = RoundedCard(root, fill_key="log_bg", outline_key="border", stretch=True)
        self._log_card = lc
        lc.pack(fill="both", expand=True)
        li = lc.inner
        li.config(padx=10, pady=8)
        lhead = tk.Frame(li, bg=PAL["log_bg"])
        lhead.pack(fill="x")
        lt = tk.Label(lhead, text="CONVERSATION", font=FONT_XS,
                      bg=PAL["log_bg"], fg=PAL["muted"])
        lt.pack(side="left")
        # Clearing belongs to the log, not to a full-width button competing
        # with the primary action for attention.
        self.clear_btn = tk.Label(lhead, text="Clear", font=FONT_XS, bg=PAL["log_bg"],
                                  fg=PAL["muted"], cursor="hand2", padx=6)
        self.clear_btn.pack(side="right")
        self.clear_btn.bind("<Button-1>", lambda _e: self._clear_log())
        self.clear_btn.bind("<Enter>", lambda e: e.widget.config(fg=PAL["accent"]))
        self.clear_btn.bind("<Leave>", lambda e: e.widget.config(fg=PAL["muted"]))
        lbody = tk.Frame(li, bg=PAL["log_bg"])
        lbody.pack(fill="both", expand=True, pady=(4, 0))
        self.log = tk.Text(lbody, height=4, font=_mono(), bg=PAL["log_bg"],
                           fg=PAL["log_fg"], insertbackground=PAL["log_fg"],
                           relief="flat", wrap="word")
        scroll = tk.Scrollbar(lbody, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # Agent prose gets the logo blue; it is neither PRISM's own output nor
        # an error, and colouring it red on a stray word like "error handling"
        # made ordinary commentary look like a failure.
        self.log.tag_config("agent", foreground=PAL["accent_text"])
        self.log.tag_config("tool", foreground=PAL["muted"])
        self.log.tag_config("ok", foreground=PAL["good"])
        self.log.tag_config("warn", foreground=PAL["warn"])
        self.log.tag_config("err", foreground=PAL["bad"])
        self.log.tag_config("ph", foreground=PAL["log_ph"])
        self._verdict_cache = ""
        self._log_placeholder = True
        self._show_placeholder()
        self._draw_badge()
        self._refresh_detection()
        # Re-scan whenever the folder changes, typed as well as browsed —
        # otherwise a hand-edited path runs against the previous folder's mode.
        self._detect_job = None
        self.proj_var.trace_add("write", self._on_proj_changed)
        self._reset_progress(silent=True)
        self._load_models_async()

    def _place_midrow(self):
        """Grid the mapping and model cards for the current mode.

        Single-repo projects never map anything, so rather than leaving half
        the row empty the model card spans it.
        """
        show_map = self.mode == "multi"
        if show_map:
            self.map_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            self.model_card.grid(row=0, column=1, columnspan=1, sticky="nsew", padx=(5, 0))
        else:
            self.map_card.grid_remove()
            self.model_card.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0)

    # ----- badge -----
    def _draw_badge(self):
        c = self._badge
        c.delete("all")
        c.config(bg=PAL["page"])
        _rr(c, 2, 2, 30, 30, 8, PAL["badge_bg"], None)
        c.create_text(16, 16, text="◇", fill=PAL["badge_fg"], font=("Segoe UI", 13, "bold"))

    # ----- log -----
    def _show_placeholder(self):
        self.log.delete("1.0", "end")
        self.log.insert("end", "Waiting to start…", "ph")
        self._log_placeholder = True

    def _clear_log(self):
        self._show_placeholder()

    def _append(self, line, tag=None):
        if self._log_placeholder:
            self.log.delete("1.0", "end")
            self._log_placeholder = False
        self.log.insert("end", line + "\n", tag or ())
        self.log.see("end")

    # ----- models -----
    def _load_models_async(self):
        """Feed the picker from the engine's model list without freezing the UI.

        The worker thread never touches Tk (not thread-safe) — it posts the
        list to the main-thread queue consumed by _drain_logs.
        """
        self.model_count.config(text="loading…")
        def work():
            models = list_available_models()
            # App-level message, not a job's: the drain unpacks three
            # fields, so carry a null job id rather than a short tuple.
            self.log_q.put((None, "models", models))
        threading.Thread(target=work, daemon=True).start()

    def _apply_models(self, models):
        self.model_picker.set_models(models)
        n_prov = len({m.partition("/")[0] for m in models}) if models else 0
        self.model_count.config(
            text=f"{len(models)} models · {n_prov} providers"
            if models else "could not list models")

    # ----- repos: scan root, single mode or BE/FE mapping -----
    def _browse(self):
        d = filedialog.askdirectory(title="Select project root folder")
        if d:
            self.repo_var.set("")
            self.proj_var.set(d)  # the trace re-runs detection
            self._refresh_detection()

    def _on_proj_changed(self, *_a):
        """Debounced re-scan — typing a path fires one write per keystroke."""
        # The repo name is derived from the folder, so it must not outlive it.
        self.repo_var.set("")
        if getattr(self, "_detect_job", None) is not None:
            try:
                self.after_cancel(self._detect_job)
            except Exception:  # noqa: BLE001
                pass
        self._detect_job = self.after(400, self._detect_now)

    def _detect_now(self):
        self._detect_job = None
        try:
            self._refresh_detection()
        except Exception as e:  # noqa: BLE001
            self._set_hint(f"Could not scan folder: {e}", "bad")

    def _refresh_detection(self):
        """Scan the root folder for git clones and pick single vs multi mode.

        - Root folder itself is a repo → single mode, mapping skipped.
        - 0–1 subdir clones → single mode (0 = API mode, repo name typed in).
        - 2+ subdir clones → multi mode: user confirms BE/FE mapping.
        Nothing is hardcoded — everything comes from `.git` discovery plus
        generic backend/frontend name heuristics.
        """
        raw = self.proj_var.get().strip()
        if not raw:
            # Nothing chosen yet: stay inert rather than scanning some default.
            self.mode = "single"
            self.detected = []
            self.single_path = None
            self._place_midrow()
            self._rebuild_target_row()
            self._set_hint("Select your project folder to begin — "
                           "PRISM scans it for git clones.", "muted")
            return
        base = Path(raw)
        if not base.is_dir():
            # Reset to the safest mode so a stale multi-repo mapping from a
            # previous folder can never be used for the next run.
            self.mode = "single"
            self.detected = []
            self.single_path = None
            self._place_midrow()
            self._rebuild_target_row()
            self._set_hint("Select a valid root folder.", "bad")
            return
        root_is_repo = (base / ".git").exists()
        subs = [(n, p) for n, p in detect_local_repos(str(base))
                if Path(p) != base]
        if root_is_repo or len(subs) <= 1:
            self.mode = "single"
            self.detected = subs
            if root_is_repo:
                self.single_path = str(base)
                if not self.repo_var.get().strip():
                    self.repo_var.set(base.name)
                self._set_hint(f"Single repository: {base.name} — mapping skipped.", "muted")
            elif len(subs) == 1:
                self.single_path = subs[0][1]
                if not self.repo_var.get().strip():
                    self.repo_var.set(subs[0][0])
                self._set_hint(f"Single repository detected: {subs[0][0]}.", "muted")
            else:
                self.single_path = None
                self._set_hint("No local clones detected — review will use the "
                               "CodeCommit API. Type the repo name manually.", "warn")
            self._place_midrow()
        else:
            self.mode = "multi"
            self.detected = subs
            names = [n for n, _ in subs]
            self.be_picker.set_models(names)
            self.fe_picker.set_models(names)
            self._auto_guess(names)
            self._place_midrow()
            self._set_hint(f"{len(subs)} clones detected — confirm backend / frontend "
                           f"mapping below.", "muted")
        self._rebuild_target_row()

    def _auto_guess(self, names):
        """Pre-fill BE/FE mapping from generic folder-name conventions."""
        be_hit = next((n for n in names if _guess_role(n) == "BE"), "")
        fe_hit = next((n for n in names if _guess_role(n) == "FE"), "")
        if be_hit and not fe_hit and len(names) == 2:
            fe_hit = next(n for n in names if n != be_hit)
        if fe_hit and not be_hit and len(names) == 2:
            be_hit = next(n for n in names if n != fe_hit)
        if be_hit:
            self.be_picker.set_custom(be_hit)
        if fe_hit:
            self.fe_picker.set_custom(fe_hit)

    def _rebuild_target_row(self):
        """Left cell of the target row: repo textbox (single) or BE/FE toggle (multi)."""
        for w in self.leftbox.winfo_children():
            w.destroy()
        self.tbe_btn = None
        self.tfe_btn = None
        if self.mode == "multi":
            self._lab(self.leftbox, "Review target").pack(anchor="w", pady=(0, 4))
            brow = tk.Frame(self.leftbox, bg=PAL["card"])
            brow.pack(anchor="w")
            self.tbe_btn = RoundedButton(brow, text="Backend", height=30, width=108,
                                         font=FONT_S,
                                         command=lambda: self._set_target("BE"))
            self.tfe_btn = RoundedButton(brow, text="Frontend", height=30, width=108,
                                         font=FONT_S,
                                         command=lambda: self._set_target("FE"))
            self.tbe_btn.pack(side="left", padx=(0, 6))
            self.tfe_btn.pack(side="left")
            self._set_target(self.target_var.get() or "BE")
        else:
            self._lab(self.leftbox, "CodeCommit repo").pack(anchor="w", pady=(0, 4))
            self._entry(self.leftbox, self.repo_var).pack(fill="x", ipady=3)

    def _set_target(self, which):
        self.target_var.set(which)
        if self.tbe_btn is not None:
            self.tbe_btn.set_selected(which == "BE")
            self.tfe_btn.set_selected(which == "FE")

    def _mapping(self):
        """Return {BE: (name, path), FE: (name, path)} for mapped clones."""
        by_name = dict(self.detected)
        out = {}
        for role, picker in (("BE", self.be_picker), ("FE", self.fe_picker)):
            name = picker.get().strip()
            if name and name in by_name:
                out[role] = (name, by_name[name])
        return out

    def _set_hint(self, text, color):
        self.clone_hint.config(text=text, fg=PAL[color])

    # ----- progress -----
    def _reset_progress(self, silent=False):
        self.stage_state = {sid: "pending" for sid, _ in STAGE_DEFS}
        self.stage_state[STAGE_SYNC] = "pending"  # tracked; shares the Merge caption
        self.seg.reset()
        if not silent:
            self.pill.set("Running…", "accent")
            self._paint_verdict()

    def _on_progress(self, stage, state):
        if stage not in self.stage_state:
            return
        self.stage_state[stage] = state
        if stage == STAGE_SYNC:
            # Sync has no cell of its own — it borrows the Merge cell.
            if state == "active":
                self.seg.set_stage(STAGE_MERGE, "active", "Syncing…")
            elif state == "done":
                self.seg.set_stage(STAGE_MERGE, "active")
            elif state == "error":
                self.seg.set_stage(STAGE_MERGE, "error")
            return
        if stage == STAGE_MERGE and self.stage_state.get(STAGE_SYNC) in ("active", "error"):
            # Sync owns the cell while running, and keeps it after a failure —
            # a trailing "merge skipped" must not paint over the error state.
            return
        if stage == STAGE_MERGE:
            self.seg.set_stage(stage, state)
        else:
            self.seg.set_stage(stage, state)

    def _set_status(self, txt, color):
        self.pill.set(txt, color)

    # ----- verdict / impact -----
    def _paint_impact(self, raw):
        """Show just `N/10`, prefixed by an icon scaled to the score."""
        m = _IMPACT_RE.search(raw or "")
        if not m:
            self.impact_val.config(text="—", fg=PAL["text"])
            return
        score, out_of = m.group(1), m.group(2)
        icon, colour = _impact_style(score, out_of)
        label = f"{icon}  {score}/{out_of}".strip()
        self.impact_val.config(text=label, fg=PAL[colour])

    def _paint_verdict(self):
        try:
            self.verdict_val.config(fg=PAL[_verdict_colour(self._verdict_key)])
        except Exception:  # noqa: BLE001
            pass

    # ----- run -----
    # ---------------- navigation ----------------
    def _show_screen(self, screen):
        for s in (self.jobs_screen, self.new_screen, self.detail_screen):
            if s is not screen and s.winfo_manager():
                s.pack_forget()
        if not screen.winfo_manager():
            screen.pack(fill="both", expand=True)
        self.screen = screen
        if screen is self.jobs_screen:
            if self.back_btn.winfo_manager():
                self.back_btn.pack_forget()
        elif not self.back_btn.winfo_manager():
            self.back_btn.pack(side="right", padx=(0, 10))

    def show_jobs(self):
        # Popups are position-anchored Toplevels; left open they would float
        # over a screen that no longer exists.
        self._close_pickers()
        self.selected_job_id = None
        self._show_screen(self.jobs_screen)
        self._refresh_jobs_list()
        self._refresh_pill()

    def show_new(self):
        if self.last_spec is not None:
            self._populate_form(self.last_spec)
        self._show_screen(self.new_screen)

    def show_detail(self, job_id):
        job = self.manager.jobs.get(job_id)
        if job is None:
            return self.show_jobs()
        self._close_pickers()
        self.selected_job_id = job_id
        self._show_screen(self.detail_screen)
        self._render_job(job)
        self._refresh_pill()

    def _close_pickers(self):
        for picker in (getattr(self, "be_picker", None),
                       getattr(self, "fe_picker", None),
                       getattr(self, "model_picker", None)):
            if picker is not None:
                picker.close()

    # ---------------- jobs list ----------------
    def _refresh_jobs_list(self):
        inner = self.jobs_list.inner
        for job_id, row in list(self.rows.items()):
            if job_id not in self.manager.jobs:
                row.destroy()
                del self.rows[job_id]
        for job in self.manager.jobs.values():
            row = self.rows.get(job.id)
            if row is None:
                row = JobRow(inner, job, on_open=self.show_detail,
                             on_stop=self._stop_job, on_remove=self._remove_job)
                self.rows[job.id] = row
            if not row.winfo_manager():
                row.pack(fill="x", pady=(0, 6))
            row.refresh()
        if self.manager.jobs:
            if self.jobs_empty.winfo_manager():
                self.jobs_empty.pack_forget()
        elif not self.jobs_empty.winfo_manager():
            self.jobs_empty.pack(pady=40)

    def _refresh_pill(self):
        """Header tally — app-level, since no single job owns the header."""
        jobs = self.manager.jobs.values()
        asking = sum(1 for j in jobs if j.status == J.NEEDS_INPUT)
        running = sum(1 for j in jobs if j.status in (J.RUNNING, J.STOPPING))
        queued = sum(1 for j in jobs if j.status == J.QUEUED)
        if asking:
            self.pill.set(f"{asking} need input", "warn", spin=bool(running))
        elif running or queued:
            txt = f"{running} running" + (f" · {queued} queued" if queued else "")
            self.pill.set(txt, "accent", spin=bool(running))
        else:
            self.pill.set("Idle", "muted")

    # ---------------- creating a job ----------------
    def _build_spec(self):
        """Validate the form and snapshot it, or return None after warning."""
        pr = _entry_value(self.pr_entry).strip()
        proj = self.proj_var.get().strip()
        if not pr.isdigit():
            show_warning(self, "PR id", "Enter a numeric CodeCommit PR id.")
            return None
        if not Path(proj).is_dir():
            show_warning(self, "Project folder", "Pick a valid root folder first.")
            return None
        if self.mode == "multi":
            mapping = self._mapping()
            target = self.target_var.get() or "BE"
            picked = mapping.get(target)
            if not picked:
                show_warning(
                    self, "Mapping",
                    f"Select the {'Backend' if target == 'BE' else 'Frontend'} clone "
                    f"in Repository mapping first.")
                return None
            other = mapping.get("FE" if target == "BE" else "BE")
            if other and other[1] == picked[1]:
                show_warning(self, "Mapping", "Backend and Frontend point to the same clone.")
                return None
            repo, local_repo = Path(picked[1]).name, picked[1]
        else:
            repo = self.repo_var.get().strip()
            if not repo:
                show_warning(self, "Repository", "Enter the CodeCommit repository name.")
                return None
            local_repo = self.single_path
        return J.JobSpec(
            project_dir=proj, repo_name=repo, pr_id=pr, local_repo=local_repo,
            region=self.region_var.get().strip() or REGION_DEFAULT,
            model=self.model_picker.get() or None,
            do_update_desc=self.upd_var.get(), do_merge=self.mrg_var.get(),
            do_sync=self.syn_var.get(), dry_run=self.dry_var.get())

    def _populate_form(self, spec):
        """Seed the form from a previous job — usually only the PR id changes."""
        self.proj_var.set(spec.project_dir)
        self.region_var.set(spec.region)
        self.upd_var.set(spec.do_update_desc)
        self.mrg_var.set(spec.do_merge)
        self.syn_var.set(spec.do_sync)
        self.dry_var.set(spec.dry_run)
        self._refresh_detection()
        if self.mode != "multi":
            self.repo_var.set(spec.repo_name)
        self.pr_entry.delete(0, "end")
        _restore_placeholder(self.pr_entry)

    def _start(self):
        spec = self._build_spec()
        if spec is None:
            return
        sharer = self.manager.shares_clone_with(spec)
        if sharer is not None and not ask_confirm(
                self, "Same clone",
                f"{sharer.spec.label} is already using this clone.\n\n"
                f"Both jobs can run — git operations on a shared checkout are "
                f"serialised — but one may wait for the other.",
                confirm="Start anyway", cancel="Cancel"):
            return
        try:
            job = self.manager.create(spec)
        except J.DuplicateJob as e:
            show_warning(self, "Already running", str(e))
            return
        self.last_spec = spec
        self.manager.pump()
        self._refresh_jobs_list()
        self._refresh_pill()      # the job just went RUNNING; start the spinner
        self.show_detail(job.id)

    # ---------------- per-job control ----------------
    def _stop_job(self, job_id):
        job = self.manager.stop(job_id)
        if job is None:
            return
        if job.status == J.STOPPED:      # was queued, never started
            job.append_log("■ Removed from the queue before it started.", "warn")
        else:
            job.append_log("\n■ Stopping — terminating the current step…", "warn")
        if job_id == self.selected_job_id:
            self._render_job(job)
        self._refresh_jobs_list()
        self._refresh_pill()

    def _stop(self):
        if self.selected_job_id is not None:
            self._stop_job(self.selected_job_id)

    def _remove_job(self, job_id):
        job = self.manager.jobs.get(job_id)
        if job is not None and job.is_active and not ask_confirm(
                self, "Stop and remove",
                f"{job.label} is still running.\n\nStopping it terminates the "
                f"reviewer and git processes it started. Its log and verdict are "
                f"discarded — PRISM keeps nothing on disk.",
                confirm="Stop and remove", cancel="Keep it", tone="bad"):
            return
        self.manager.remove(job_id)
        if self.selected_job_id == job_id:
            self.show_jobs()
        else:
            self._refresh_jobs_list()
        self.manager.pump()
        self._refresh_pill()

    # ---------------- rendering a job onto the detail widgets ----------------
    def _render_job(self, job):
        """Full repaint of the detail screen from the model.

        Everything here reads the Job rather than replaying events, so a job
        that ran entirely while another was on screen shows up correctly.
        """
        self.detail_title.config(text=job.summary_line())
        self.stage_state = dict(job.stages)
        self.seg.reset()
        for sid, state in job.stages.items():
            if sid == STAGE_SYNC:
                continue
            self.seg.set_stage(sid, state)
        if job.stages.get(STAGE_SYNC) == "active":
            self.seg.set_stage(STAGE_MERGE, "active", "Syncing…")
        self.verdict_val.config(text=job.verdict_raw or "Not run yet")
        self._verdict_key = job.verdict_key
        self._paint_verdict()
        self._paint_impact(job.impact)
        self.run_btn.set_text("▶  Start Prisming")
        self.stop_btn.set_enabled(job.is_active)
        self._render_log(job)
        if job.pending_question:
            # The ask event already fired while this job was unselected, so the
            # panel has to be driven from stored state, not from the event.
            self.agent_panel.present(job.pending_question)
            if not self.agent_panel.winfo_manager():
                self.agent_panel.pack(fill="x", pady=(0, 6), before=self._log_card)
        else:
            self._hide_agent()

    def _render_log(self, job):
        self.log.delete("1.0", "end")
        if not job.log:
            self._show_placeholder()
            return
        self._log_placeholder = False
        # Group consecutive same-tag lines so a full buffer is a few hundred
        # inserts rather than thousands.
        run, run_tag = [], object()
        for text, tag in job.log:
            if tag != run_tag and run:
                self.log.insert("end", "\n".join(run) + "\n", run_tag or ())
                run = []
            run_tag = tag
            run.append(text)
        if run:
            self.log.insert("end", "\n".join(run) + "\n", run_tag or ())
        self.log.see("end")

    # ---------------- agent conversation ----------------
    def _hide_agent(self):
        if self.agent_panel.winfo_manager():
            self.agent_panel.pack_forget()

    def _answer_agent(self, message):
        job = self.manager.jobs.get(self.selected_job_id)
        if job is None or not job.pending_question:
            return
        job.pending_question = None
        if job.status == J.NEEDS_INPUT:
            job.status = J.RUNNING
        self._hide_agent()
        job.ask.resolve(message)
        self._refresh_jobs_list()
        self._refresh_pill()

    # ---------------- the pump ----------------
    def _drain_logs(self):
        try:
            self._drain_once()
            self._tick_animation()
        finally:
            # Always reschedule: one bad line must not kill the pump and leave
            # the UI frozen mid-run with no log and no progress.
            self._drain_job = self.after(80, self._drain_logs)

    def _on_close(self):
        """Stop the pump and every live job before the window goes away.

        daemon=True protects only the Python threads, not the opencode/git
        children they spawned, so without cancelling each job those processes
        would outlive the window.
        """
        if not self._confirm_quit():
            return
        if self._drain_job is not None:
            try:
                self.after_cancel(self._drain_job)
            except Exception:  # noqa: BLE001
                pass
            self._drain_job = None
        self.manager.stop_all()
        self.destroy()

    def _confirm_quit(self):
        """Ask before discarding work, since closing discards all of it.

        PRISM keeps nothing on disk, so the window is the only place a verdict
        or a transcript exists. Closing it is therefore destructive in a way a
        window close usually is not, and the prompt says exactly what goes.
        """
        active = self.manager.active_jobs()
        finished = [j for j in self.manager.jobs.values() if j.is_terminal]
        if not active and not finished:
            return True            # nothing to lose; don't nag
        lines = ["PRISM is stateless — nothing is saved to disk, so closing "
                 "this window discards everything below."]
        if active:
            started = "it started" if len(active) == 1 else "they started"
            lines.append(f"• {_plural(len(active), 'unfinished job')} will be "
                         f"stopped, and the reviewer and git processes "
                         f"{started} terminated.")
        # A job past the review stage may be mid-write; worth calling out
        # separately because that is the only case with an effect outside PRISM.
        writing = [j for j in active
                   if any(j.stages.get(s) == "active"
                          for s in (STAGE_DESCRIBE, STAGE_MERGE, STAGE_SYNC))]
        if writing:
            verb = "is" if len(writing) == 1 else "are"
            lines.append(f"• {len(writing)} of them {verb} updating a PR description "
                         f"or merging. Stopping is checked between steps, so a call "
                         f"already in flight with AWS may still complete.")
        if finished:
            whose = "its verdict and conversation" if len(finished) == 1 \
                else "their verdicts and conversations"
            lines.append(f"• {_plural(len(finished), 'finished job')} will be "
                         f"discarded, including {whose}.")
        lines.append("Do you still want to quit?")
        return ask_confirm(self, "Quit PRISM?", "\n\n".join(lines),
                           confirm="Quit and discard", cancel="Stay open",
                           tone="bad")

    def _drain_once(self):
        touched = False
        processed = 0
        try:
            # Bounded per tick: a job flooding output must not keep the pump
            # inside one tick and freeze the UI.
            while processed < 500:
                job_id, kind, payload = self.log_q.get_nowait()
                processed += 1
                if kind == "models":
                    self._apply_models(payload)
                    continue
                job = self.manager.jobs.get(job_id)
                if job is None:
                    continue          # dismissed while its worker was in flight
                try:
                    if self._handle(job, kind, payload):
                        touched = True
                except Exception:  # noqa: BLE001
                    # One job's bad payload must not mask the others this tick.
                    continue
        except queue.Empty:
            pass
        if touched:
            self._refresh_pill()
            if self.screen is self.jobs_screen:
                self._refresh_jobs_list()

    def _tick_animation(self):
        """Advance spinners. Idle when nothing is working, so a settled app
        costs nothing — the pump still runs, it just has no frames to draw."""
        working = [j for j in self.manager.jobs.values()
                   if j.status in (J.RUNNING, J.STOPPING)]
        if not working:
            self._anim = 0
            return
        self._anim = (self._anim + 1) % 12
        self.pill.tick(self._anim)
        if self.screen is self.jobs_screen:
            for job in working:
                row = self.rows.get(job.id)
                if row is not None:
                    row.tick(self._anim)

    def _handle(self, job, kind, payload):
        """Apply one message to the model, then to the screen if it is showing.

        Returns True when the jobs list / header need a repaint.
        """
        shown = job.id == self.selected_job_id and self.screen is self.detail_screen
        if kind == "log":
            line, tag = payload if isinstance(payload, tuple) else (payload, None)
            text = line.strip()
            if tag is None:
                tag = _classify(text)
            job.append_log(line, tag)
            payload = line
            if shown:
                self._append(line, tag)
            if text.startswith("◆ Verdict:"):
                m = re.match(r"◆ Verdict:\s*(.*?)\s{2,}Impact:\s*(.*)", text)
                job.verdict_raw = (m.group(1).strip() if m
                                   else text.replace("◆ ", "").strip())
                job.impact = m.group(2).strip() if m else ""
                job.verdict_key = _verdict_key_of(job.verdict_raw)
                if shown:
                    self.verdict_val.config(text=job.verdict_raw)
                    self._verdict_key = job.verdict_key
                    self._paint_verdict()
                    self._paint_impact(job.impact)
                return True
            return False
        if kind == "progress":
            stage, state = payload
            job.stages[stage] = state
            if shown:
                self.stage_state = job.stages
                self._on_progress(stage, state)
            return False
        if kind == "ask":
            job.pending_question = payload
            job.status = J.NEEDS_INPUT
            if shown:
                self.agent_panel.present(payload)
                if not self.agent_panel.winfo_manager():
                    self.agent_panel.pack(fill="x", pady=(0, 6), before=self._log_card)
            return True
        if kind in ("done", "stopped", "error"):
            if kind == "done":
                job.status = J.DONE
                job.result = payload
                job.append_log(f"\n—— finished: {payload} ——", "ok")
            elif kind == "stopped":
                job.status = J.STOPPED
                job.append_log("\n■ Run stopped.", "warn")
            else:
                job.status = J.ERROR
                job.error = payload
                job.append_log(f"\n⛔ ERROR: {payload}", "err")
            for sid in [s for s, _ in STAGE_DEFS]:
                if job.stages.get(sid) == "active":
                    job.stages[sid] = "error"
            job.pending_question = None
            if shown:
                self._render_job(job)
            self.manager.pump()     # a slot just freed
            return True
        return False


class _BadgeProxy:
    """Gives the static logo canvas a refresh_theme() hook."""

    def __init__(self, app):
        self.app = app

    def refresh_theme(self):
        try:
            self.app._draw_badge()
        except Exception:  # noqa: BLE001
            pass


def _default_project_dir():
    """Working folder to start on: deliberately none.

    PRISM used to guess — the parent of the checkout from source, the current
    directory when packaged. Launched from a desktop shortcut the working
    directory is $HOME, so the app scanned the user's entire home on startup
    and pre-filled the repo box from whatever clone it happened to find there
    (a dotfile checkout like ~/.nvm, say). Scanning a directory the user never
    chose, and naming a CodeCommit repo off the back of it, is not a guess the
    tool should be making. Start empty and wait for an explicit choice.
    """
    return ""


def _add_placeholder(entry, text):
    entry._ph_text = text  # noqa: SLF001 - so it can be restored later
    entry.insert(0, text)
    entry._has_ph = True  # noqa: SLF001
    entry.config(fg=PAL["muted"])
    def on_in(_e):
        if getattr(entry, "_has_ph", False):
            entry.delete(0, "end")
            entry._has_ph = False
            entry.config(fg=PAL["text"])
    def on_out(_e):
        if not entry.get():
            entry.insert(0, text)
            entry._has_ph = True
            entry.config(fg=PAL["muted"])
    entry.bind("<FocusIn>", on_in)
    entry.bind("<FocusOut>", on_out)


def _restore_placeholder(entry):
    """Put a cleared field back into its placeholder state.

    Used when the new-job form is re-seeded from the previous job: everything
    else carries over, but the PR id must always be typed fresh.
    """
    text = getattr(entry, "_ph_text", None)
    if text is None:
        return
    entry.delete(0, "end")
    entry.insert(0, text)
    entry._has_ph = True  # noqa: SLF001
    entry.config(fg=PAL["muted"])


def _entry_value(entry):
    if getattr(entry, "_has_ph", False):
        return ""
    return entry.get()


# Impact is a 1-10 blast-radius score. Only the score belongs on the card: the
# agent's one-line reason is often a full clause, and rendering it here stretched
# the row. The reason is still written to the log, so nothing is lost.
_IMPACT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _impact_style(score, out_of=10):
    """Icon + palette key for a score, by its share of the scale.

    Same glyph family as the verdict so the two cards read as one language,
    and these four are already proven to render in this Tk build.
    """
    try:
        ratio = float(score) / float(out_of or 10)
    except (TypeError, ValueError, ZeroDivisionError):
        return "", "text"
    if ratio <= 0.3:
        return "✅", "good"        # 1-3  isolated
    if ratio <= 0.6:
        return "⚠️", "warn"        # 4-6  moderate
    if ratio <= 0.8:
        return "🔴", "bad"         # 7-8  high
    return "⛔", "bad"             # 9-10 critical


def _plural(n, singular, plural=None):
    """"1 job" / "3 jobs" — these strings are shown to people."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _classify(text):
    """Tag for a line PRISM emitted itself.

    Marker-driven on purpose. The previous version tagged any line containing
    "error" or "fail" as a failure, which painted ordinary agent commentary —
    "its error handling", "the failing test" — in alarming red.
    """
    if text.startswith(("◆", "✅")):
        return "ok"
    if text.startswith(("⚠", "■")):
        return "warn"
    if text.startswith("⛔") or "ERROR:" in text or "Traceback" in text:
        return "err"
    return None


def _verdict_colour(key):
    """Palette key for a verdict. Shared by the card and the jobs-list row so
    the two can never disagree about how severe a verdict looks."""
    return {"approve": "good", "approve-with-comments": "warn",
            "request-changes": "bad", "block": "bad"}.get(key, "text")


def _verdict_key_of(line):
    """Verdict key for colour-coding. Shares the orchestrator's classifier so
    the card can never disagree with the merge decision."""
    key = normalise_verdict(line or "")
    return "" if key == "unknown" else key


_BE_RE = re.compile(r"(^|[-_.])be([-_.]|$)|backend", re.IGNORECASE)
_FE_RE = re.compile(r"(^|[-_.])fe([-_.]|$)|frontend", re.IGNORECASE)


def _guess_role(folder_name):
    """Guess 'BE' / 'FE' from a clone folder name using generic conventions
    (a -be/-fe suffix or a backend/frontend word). Returns '' when unsure —
    the user then confirms manually. No project names are hardcoded."""
    be = bool(_BE_RE.search(folder_name or ""))
    fe = bool(_FE_RE.search(folder_name or ""))
    if be and not fe:
        return "BE"
    if fe and not be:
        return "FE"
    return ""


if __name__ == "__main__":
    App().mainloop()
