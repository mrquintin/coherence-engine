"""Coherence Engine — Native macOS GUI Application.

A complete graphical interface for analyzing text coherence, built with tkinter.
Designed to feel native on macOS with proper dark mode support, keyboard
shortcuts, and a polished layout.
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import threading
import json
import os


# ---------------------------------------------------------------------------
# Color palette and styling
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#1e1e2e",
    "bg_secondary": "#252536",
    "bg_input": "#2a2a3c",
    "bg_hover": "#313145",
    "fg": "#cdd6f4",
    "fg_dim": "#7f849c",
    "fg_bright": "#ffffff",
    "accent": "#89b4fa",
    "accent_hover": "#74c7ec",
    "green": "#a6e3a1",
    "yellow": "#f9e2af",
    "red": "#f38ba8",
    "peach": "#fab387",
    "border": "#45475a",
    "bar_filled": "#89b4fa",
    "bar_empty": "#313145",
}

FONT_FAMILY = "SF Pro Text"
FONT_FAMILY_MONO = "SF Mono"
FONT_FALLBACK = "Helvetica"
FONT_MONO_FALLBACK = "Menlo"


def _resolve_fonts():
    """Pick fonts that exist on this system."""
    root = tk.Tk()
    root.withdraw()
    available = set(tk.font.families())
    root.destroy()

    body = FONT_FAMILY if FONT_FAMILY in available else FONT_FALLBACK
    mono = FONT_FAMILY_MONO if FONT_FAMILY_MONO in available else FONT_MONO_FALLBACK
    return body, mono


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------

class CoherenceEngineApp:
    """Main GUI application."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Coherence Engine")
        self.root.geometry("980x820")
        self.root.minsize(780, 620)
        self.root.configure(bg=COLORS["bg"])

        # macOS-specific window config
        try:
            self.root.tk.call("::tk::unsupported::MacWindowStyle", "style",
                              self.root._w, "moveableModal", "")
        except tk.TclError:
            pass

        self._setup_fonts()
        self._setup_styles()
        self._build_ui()
        self._bind_shortcuts()

        self._scorer = None
        self._result = None
        self._delegation_result = None
        self._analyzing = False
        self.auto_delegate_var = tk.BooleanVar(value=True)
        self.force_parallel_var = tk.IntVar(value=0)
        self.agent_list_var = tk.StringVar(value="planner,critic,builder,synthesizer")
        self.auto_threshold_words_var = tk.IntVar(value=1000)
        self.auto_threshold_chars_var = tk.IntVar(value=7000)

    # -- Fonts & Styles -------------------------------------------------------

    def _setup_fonts(self):
        import tkinter.font as tkfont
        available = set(tkfont.families(self.root))

        body = FONT_FAMILY if FONT_FAMILY in available else FONT_FALLBACK
        mono = FONT_FAMILY_MONO if FONT_FAMILY_MONO in available else FONT_MONO_FALLBACK

        self.font_body = (body, 13)
        self.font_body_bold = (body, 13, "bold")
        self.font_small = (body, 11)
        self.font_small_dim = (body, 11)
        self.font_heading = (body, 20, "bold")
        self.font_subheading = (body, 15, "bold")
        self.font_score = (mono, 48, "bold")
        self.font_mono = (mono, 12)
        self.font_mono_small = (mono, 11)
        self.font_button = (body, 13, "bold")

    def _setup_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=COLORS["bg"], foreground=COLORS["fg"],
                         font=self.font_body)

        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Secondary.TFrame", background=COLORS["bg_secondary"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["fg"],
                         font=self.font_body)
        style.configure("Dim.TLabel", foreground=COLORS["fg_dim"], font=self.font_small)
        style.configure("Heading.TLabel", foreground=COLORS["fg_bright"],
                         font=self.font_heading)
        style.configure("Subheading.TLabel", foreground=COLORS["fg_bright"],
                         font=self.font_subheading)

        style.configure("Accent.TButton",
                         background=COLORS["accent"],
                         foreground=COLORS["bg"],
                         font=self.font_button,
                         padding=(20, 10))
        style.map("Accent.TButton",
                   background=[("active", COLORS["accent_hover"]),
                               ("disabled", COLORS["border"])])

        style.configure("Secondary.TButton",
                         background=COLORS["bg_secondary"],
                         foreground=COLORS["fg"],
                         font=self.font_body,
                         padding=(14, 8))
        style.map("Secondary.TButton",
                   background=[("active", COLORS["bg_hover"])])

        style.configure("TNotebook", background=COLORS["bg"],
                         borderwidth=0)
        style.configure("TNotebook.Tab",
                         background=COLORS["bg_secondary"],
                         foreground=COLORS["fg_dim"],
                         padding=(16, 8),
                         font=self.font_body)
        style.map("TNotebook.Tab",
                   background=[("selected", COLORS["bg"])],
                   foreground=[("selected", COLORS["accent"])])

    # -- UI Layout ------------------------------------------------------------

    def _build_ui(self):
        # Top bar
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=24, pady=(20, 0))

        ttk.Label(top, text="Coherence Engine",
                  style="Heading.TLabel").pack(side="left")

        self.status_label = ttk.Label(top, text="Ready", style="Dim.TLabel")
        self.status_label.pack(side="right")

        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=24, pady=(12, 24))

        self._build_analyze_tab()
        self._build_results_tab()
        self._build_delegation_tab()
        self._build_layers_tab()

    def _build_analyze_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Analyze  ")

        # Input section
        input_frame = ttk.Frame(tab)
        input_frame.pack(fill="both", expand=True, padx=4, pady=(12, 0))

        header_row = ttk.Frame(input_frame)
        header_row.pack(fill="x", pady=(0, 8))

        ttk.Label(header_row, text="Input Text",
                  style="Subheading.TLabel").pack(side="left")

        btn_frame = ttk.Frame(header_row)
        btn_frame.pack(side="right")

        ttk.Button(btn_frame, text="Open File",
                   style="Secondary.TButton",
                   command=self._open_file).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="Paste",
                   style="Secondary.TButton",
                   command=self._paste_clipboard).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="Clear",
                   style="Secondary.TButton",
                   command=self._clear_input).pack(side="left")

        # Text area
        text_frame = tk.Frame(input_frame, bg=COLORS["border"],
                              highlightthickness=0)
        text_frame.pack(fill="both", expand=True)

        self.text_input = scrolledtext.ScrolledText(
            text_frame,
            wrap="word",
            font=self.font_body,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            selectbackground=COLORS["accent"],
            selectforeground=COLORS["bg"],
            relief="flat",
            padx=16,
            pady=12,
            borderwidth=0,
            highlightthickness=0,
        )
        self.text_input.pack(fill="both", expand=True, padx=1, pady=1)

        placeholder = (
            "Paste or type any text here — an essay, a pitch, a policy document, "
            "an argument — and the engine will measure its internal logical coherence.\n\n"
            "You can also drag a file here, use the Open File button, or press ⌘V to paste."
        )
        self.text_input.insert("1.0", placeholder)
        self.text_input.config(fg=COLORS["fg_dim"])
        self._placeholder_active = True
        self.text_input.bind("<FocusIn>", self._on_focus_in)
        self.text_input.bind("<FocusOut>", self._on_focus_out)

        # Word count row
        count_row = ttk.Frame(input_frame)
        count_row.pack(fill="x", pady=(6, 0))

        self.word_count_label = ttk.Label(count_row, text="0 words",
                                          style="Dim.TLabel")
        self.word_count_label.pack(side="left")
        self.text_input.bind("<KeyRelease>", self._update_word_count)

        # Delegation controls
        delegate_row = ttk.Frame(input_frame)
        delegate_row.pack(fill="x", pady=(8, 0))

        ttk.Checkbutton(
            delegate_row,
            text="Auto-fan-out large prompts",
            variable=self.auto_delegate_var,
        ).pack(side="left", padx=(0, 12))

        ttk.Label(delegate_row, text="Force parallel (0-4):", style="Dim.TLabel").pack(side="left")
        tk.Spinbox(
            delegate_row,
            from_=0,
            to=4,
            width=4,
            textvariable=self.force_parallel_var,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            relief="flat",
        ).pack(side="left", padx=(6, 12))

        ttk.Label(delegate_row, text="Agents:", style="Dim.TLabel").pack(side="left")
        tk.Entry(
            delegate_row,
            textvariable=self.agent_list_var,
            width=34,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            relief="flat",
        ).pack(side="left", padx=(6, 8))

        threshold_row = ttk.Frame(input_frame)
        threshold_row.pack(fill="x", pady=(6, 0))
        ttk.Label(threshold_row, text="Auto thresholds  words:", style="Dim.TLabel").pack(side="left")
        tk.Spinbox(
            threshold_row,
            from_=100,
            to=50000,
            increment=100,
            width=7,
            textvariable=self.auto_threshold_words_var,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            relief="flat",
        ).pack(side="left", padx=(6, 12))
        ttk.Label(threshold_row, text="chars:", style="Dim.TLabel").pack(side="left")
        tk.Spinbox(
            threshold_row,
            from_=500,
            to=200000,
            increment=500,
            width=8,
            textvariable=self.auto_threshold_chars_var,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            relief="flat",
        ).pack(side="left", padx=(6, 0))

        # Analyze button
        button_row = ttk.Frame(tab)
        button_row.pack(fill="x", padx=4, pady=(16, 12))

        self.analyze_btn = ttk.Button(
            button_row, text="▶  Analyze Coherence",
            style="Accent.TButton",
            command=self._run_analysis)
        self.analyze_btn.pack(side="left")

        self.progress_label = ttk.Label(button_row, text="", style="Dim.TLabel")
        self.progress_label.pack(side="left", padx=(16, 0))

    def _build_results_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Results  ")

        self.results_container = ttk.Frame(tab)
        self.results_container.pack(fill="both", expand=True, padx=4, pady=12)

        self._show_empty_results()

    def _show_empty_results(self):
        for w in self.results_container.winfo_children():
            w.destroy()

        msg = ttk.Label(self.results_container,
                        text="No analysis results yet.\nRun an analysis from the Analyze tab.",
                        style="Dim.TLabel", justify="center")
        msg.pack(expand=True)

    def _build_delegation_tab(self):
        self.delegation_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.delegation_tab, text="  Delegation  ")

        top = ttk.Frame(self.delegation_tab)
        top.pack(fill="x", padx=8, pady=(10, 8))

        self.delegation_status_label = ttk.Label(
            top,
            text="No delegated run yet.",
            style="Dim.TLabel",
        )
        self.delegation_status_label.pack(side="left")

        self.copy_synthesis_btn = ttk.Button(
            top,
            text="Copy Synthesis Prompt",
            style="Secondary.TButton",
            command=self._copy_synthesis_prompt,
        )
        self.copy_synthesis_btn.pack(side="right")

        self.delegation_text = scrolledtext.ScrolledText(
            self.delegation_tab,
            wrap="word",
            font=self.font_mono_small,
            bg=COLORS["bg_input"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            relief="flat",
            padx=12,
            pady=10,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.delegation_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.synthesis_text = scrolledtext.ScrolledText(
            self.delegation_tab,
            wrap="word",
            height=8,
            font=self.font_small,
            bg=COLORS["bg_secondary"],
            fg=COLORS["fg"],
            insertbackground=COLORS["accent"],
            relief="flat",
            padx=12,
            pady=10,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.synthesis_text.pack(fill="x", padx=8, pady=(0, 10))

        self._show_empty_delegation()

    def _show_empty_delegation(self):
        if not hasattr(self, "delegation_text"):
            return
        self.delegation_status_label.config(text="No delegated run yet.")
        self.delegation_text.config(state="normal")
        self.delegation_text.delete("1.0", "end")
        self.delegation_text.insert(
            "1.0",
            "No delegated run yet.\n"
            "Run analysis with auto-fan-out enabled or set Force parallel > 0.",
        )
        self.delegation_text.config(state="disabled")

        self.synthesis_text.config(state="normal")
        self.synthesis_text.delete("1.0", "end")
        self.synthesis_text.insert("1.0", "Synthesis prompt will appear here.")
        self.synthesis_text.config(state="disabled")

    def _build_layers_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="  Layers  ")

        layers_info = [
            ("Layer 1: Contradiction Detection", "0.30",
             "Detects logical contradictions between proposition pairs.\n"
             "Uses NLI (DeBERTa) when available, otherwise heuristic pattern matching."),
            ("Layer 2: Argumentation Analysis", "0.20",
             "Evaluates argument structure via Dung's abstract argumentation framework.\n"
             "Computes the grounded extension — propositions that survive all attacks."),
            ("Layer 3: Embedding Coherence", "0.20",
             "Measures semantic coherence via pairwise embedding similarity.\n"
             "Includes difference-vector analysis for cosine paradox detection."),
            ("Layer 4: Compression Coherence", "0.15",
             "Information-theoretic coherence via zlib compression.\n"
             "Coherent text compresses better jointly than separately."),
            ("Layer 5: Structural Analysis", "0.15",
             "Evaluates graph quality: connectivity, depth, isolation, circularity.\n"
             "Well-structured arguments score higher."),
        ]

        canvas = tk.Canvas(tab, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>",
                          lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        for name, weight, desc in layers_info:
            card = tk.Frame(scroll_frame, bg=COLORS["bg_secondary"],
                            highlightthickness=1,
                            highlightbackground=COLORS["border"])
            card.pack(fill="x", padx=8, pady=6)

            header = tk.Frame(card, bg=COLORS["bg_secondary"])
            header.pack(fill="x", padx=16, pady=(12, 4))

            tk.Label(header, text=name, font=self.font_body_bold,
                     bg=COLORS["bg_secondary"], fg=COLORS["fg_bright"]
                     ).pack(side="left")
            tk.Label(header, text=f"weight: {weight}", font=self.font_mono_small,
                     bg=COLORS["bg_secondary"], fg=COLORS["accent"]
                     ).pack(side="right")

            tk.Label(card, text=desc, font=self.font_small,
                     bg=COLORS["bg_secondary"], fg=COLORS["fg_dim"],
                     justify="left", anchor="w", wraplength=800
                     ).pack(fill="x", padx=16, pady=(0, 12))

    # -- Placeholder logic ----------------------------------------------------

    def _on_focus_in(self, event):
        if self._placeholder_active:
            self.text_input.delete("1.0", "end")
            self.text_input.config(fg=COLORS["fg"])
            self._placeholder_active = False

    def _on_focus_out(self, event):
        content = self.text_input.get("1.0", "end").strip()
        if not content:
            self._placeholder_active = True
            self.text_input.config(fg=COLORS["fg_dim"])
            self.text_input.insert("1.0",
                "Paste or type any text here — an essay, a pitch, a policy document, "
                "an argument — and the engine will measure its internal logical coherence.")

    # -- Actions --------------------------------------------------------------

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open Text File",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self._placeholder_active = False
                self.text_input.config(fg=COLORS["fg"])
                self.text_input.delete("1.0", "end")
                self.text_input.insert("1.0", content)
                self._update_word_count(None)
                self.status_label.config(text=f"Loaded: {os.path.basename(path)}")
            except Exception as e:
                messagebox.showerror("Error", f"Could not read file:\n{e}")

    def _paste_clipboard(self):
        try:
            content = self.root.clipboard_get()
            if content:
                self._placeholder_active = False
                self.text_input.config(fg=COLORS["fg"])
                self.text_input.delete("1.0", "end")
                self.text_input.insert("1.0", content)
                self._update_word_count(None)
        except tk.TclError:
            pass

    def _clear_input(self):
        self.text_input.delete("1.0", "end")
        self._placeholder_active = True
        self.text_input.config(fg=COLORS["fg_dim"])
        self.text_input.insert("1.0",
            "Paste or type any text here — an essay, a pitch, a policy document, "
            "an argument — and the engine will measure its internal logical coherence.")
        self.word_count_label.config(text="0 words")

    def _update_word_count(self, event):
        if self._placeholder_active:
            return
        content = self.text_input.get("1.0", "end").strip()
        words = len(content.split()) if content else 0
        self.word_count_label.config(text=f"{words} word{'s' if words != 1 else ''}")

    def _get_text(self):
        if self._placeholder_active:
            return ""
        return self.text_input.get("1.0", "end").strip()

    # -- Analysis runner ------------------------------------------------------

    def _run_analysis(self):
        text = self._get_text()
        if not text:
            messagebox.showwarning("No Input", "Please enter or load some text to analyze.")
            return

        if self._analyzing:
            return

        self._analyzing = True
        self.analyze_btn.config(state="disabled")
        self.progress_label.config(text="Analyzing...")
        self.status_label.config(text="Analyzing...")

        thread = threading.Thread(target=self._analysis_worker, args=(text,), daemon=True)
        thread.start()

    def _analysis_worker(self, text):
        try:
            from coherence_engine.core.delegation import PromptDelegationEngine

            force_parallel = int(self.force_parallel_var.get() or 0)
            force_parallel = max(0, min(4, force_parallel))
            selected_agents = [
                item.strip() for item in self.agent_list_var.get().split(",") if item.strip()
            ]
            auto_delegate = bool(self.auto_delegate_var.get())
            delegation_engine = PromptDelegationEngine(
                auto_word_threshold=int(self.auto_threshold_words_var.get()),
                auto_char_threshold=int(self.auto_threshold_chars_var.get()),
            )
            decision = delegation_engine.decide_delegation(
                prompt=text,
                force_parallel=force_parallel if force_parallel > 0 else None,
                auto_delegate=auto_delegate,
            )

            if decision.delegated:
                self.root.after(0, lambda: self.progress_label.config(
                    text=f"Delegating in parallel ({decision.target_agents})..."))
                delegated = delegation_engine.run(
                    prompt=text,
                    output_format="json",
                    force_parallel=force_parallel if force_parallel > 0 else None,
                    auto_delegate=auto_delegate,
                    selected_agents=selected_agents if selected_agents else None,
                    verbose=False,
                )
                self._delegation_result = delegated
                self._result = None
                self.root.after(0, lambda: self._display_delegation_results(delegated))
                return

            if self._scorer is None:
                self.root.after(0, lambda: self.progress_label.config(
                    text="Loading engine (first run)..."))
                from coherence_engine.core.scorer import CoherenceScorer
                from coherence_engine.config import EngineConfig
                self._scorer = CoherenceScorer(EngineConfig())

            self.root.after(0, lambda: self.progress_label.config(
                text="Running analysis layers..."))

            result = self._scorer.score(text)
            self._result = result
            self._delegation_result = None

            self.root.after(0, lambda: self._display_results(result))

        except Exception as exc:
            error_msg = str(exc)
            self.root.after(0, lambda: self._analysis_error(error_msg))

        finally:
            self.root.after(0, self._analysis_done)

    def _analysis_done(self):
        self._analyzing = False
        self.analyze_btn.config(state="normal")
        self.progress_label.config(text="")

    def _analysis_error(self, error_msg):
        self.status_label.config(text="Error")
        messagebox.showerror("Analysis Error", f"An error occurred:\n{error_msg}")

    # -- Results display ------------------------------------------------------

    def _display_results(self, result):
        self.notebook.select(1)
        self.status_label.config(text="Analysis complete")
        self._show_empty_delegation()

        for w in self.results_container.winfo_children():
            w.destroy()

        container = self.results_container

        # -- Score header
        score_frame = tk.Frame(container, bg=COLORS["bg"])
        score_frame.pack(fill="x", pady=(0, 16))

        score_val = result.composite_score
        score_color = self._score_color(score_val)

        tk.Label(score_frame, text=f"{score_val:.2f}",
                 font=self.font_score, bg=COLORS["bg"],
                 fg=score_color).pack(side="left")

        right_info = tk.Frame(score_frame, bg=COLORS["bg"])
        right_info.pack(side="left", padx=(16, 0), anchor="s", pady=(0, 8))

        tk.Label(right_info, text="/ 1.00  Composite Score",
                 font=self.font_body, bg=COLORS["bg"],
                 fg=COLORS["fg_dim"]).pack(anchor="w")

        interpretation = self._interpret(score_val)
        tk.Label(right_info, text=interpretation,
                 font=self.font_body_bold, bg=COLORS["bg"],
                 fg=score_color).pack(anchor="w")

        # -- Export buttons
        export_row = tk.Frame(right_info, bg=COLORS["bg"])
        export_row.pack(anchor="w", pady=(4, 0))

        for label, fmt in [("Copy JSON", "json"), ("Copy Text", "text"), ("Save Report", "save")]:
            btn = tk.Button(export_row, text=label, font=self.font_small,
                            bg=COLORS["bg_secondary"], fg=COLORS["fg_dim"],
                            activebackground=COLORS["bg_hover"],
                            activeforeground=COLORS["fg"],
                            relief="flat", padx=10, pady=3, cursor="hand2",
                            command=lambda f=fmt: self._export_result(f))
            btn.pack(side="left", padx=(0, 6))

        # -- Separator
        tk.Frame(container, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 16))

        # -- Layer bars
        tk.Label(container, text="Layer Breakdown", font=self.font_subheading,
                 bg=COLORS["bg"], fg=COLORS["fg_bright"]).pack(anchor="w", pady=(0, 10))

        for lr in result.layer_results:
            self._draw_layer_bar(container, lr)

        # -- Metadata
        tk.Frame(container, bg=COLORS["border"], height=1).pack(fill="x", pady=(16, 16))

        meta_frame = tk.Frame(container, bg=COLORS["bg"])
        meta_frame.pack(fill="x")

        struct = result.argument_structure
        n_claims = len(struct.claims) if struct else 0
        n_premises = len(struct.premises) if struct else 0
        n_total = struct.n_propositions if struct else 0
        n_contra = len(result.contradictions)
        elapsed = result.metadata.get("elapsed_seconds", "?")
        embedder = result.metadata.get("embedder", "unknown")

        stats = [
            ("Propositions", str(n_total)),
            ("Claims", str(n_claims)),
            ("Premises", str(n_premises)),
            ("Contradictions", str(n_contra)),
            ("Time", f"{elapsed}s"),
            ("Embedder", embedder),
        ]

        for i, (label, value) in enumerate(stats):
            col = tk.Frame(meta_frame, bg=COLORS["bg"])
            col.pack(side="left", padx=(0, 24))
            tk.Label(col, text=label, font=self.font_small,
                     bg=COLORS["bg"], fg=COLORS["fg_dim"]).pack(anchor="w")
            tk.Label(col, text=value, font=self.font_body_bold,
                     bg=COLORS["bg"], fg=COLORS["fg"]).pack(anchor="w")

        # -- Contradictions list
        if result.contradictions:
            tk.Frame(container, bg=COLORS["border"], height=1).pack(fill="x", pady=(16, 16))
            tk.Label(container, text=f"Contradictions Detected ({n_contra})",
                     font=self.font_subheading, bg=COLORS["bg"],
                     fg=COLORS["red"]).pack(anchor="w", pady=(0, 8))

            for i, c in enumerate(result.contradictions[:10], 1):
                card = tk.Frame(container, bg=COLORS["bg_secondary"],
                                highlightthickness=1,
                                highlightbackground=COLORS["border"])
                card.pack(fill="x", pady=3)

                tk.Label(card, text=f"#{i}", font=self.font_mono_small,
                         bg=COLORS["bg_secondary"], fg=COLORS["red"],
                         width=4).pack(side="left", padx=(12, 0), pady=8)

                text_col = tk.Frame(card, bg=COLORS["bg_secondary"])
                text_col.pack(side="left", fill="x", expand=True, padx=8, pady=8)

                a_text = getattr(c, 'prop_a_text', '?')[:100]
                b_text = getattr(c, 'prop_b_text', '?')[:100]
                conf = getattr(c, 'confidence', 0)

                tk.Label(text_col, text=f'"{a_text}"',
                         font=self.font_small, bg=COLORS["bg_secondary"],
                         fg=COLORS["fg"], anchor="w", wraplength=700
                         ).pack(anchor="w")
                tk.Label(text_col, text="  vs.",
                         font=self.font_small, bg=COLORS["bg_secondary"],
                         fg=COLORS["fg_dim"]).pack(anchor="w")
                tk.Label(text_col, text=f'"{b_text}"',
                         font=self.font_small, bg=COLORS["bg_secondary"],
                         fg=COLORS["fg"], anchor="w", wraplength=700
                         ).pack(anchor="w")

                tk.Label(card, text=f"{conf:.0%}",
                         font=self.font_mono_small, bg=COLORS["bg_secondary"],
                         fg=COLORS["peach"]).pack(side="right", padx=12)

            if n_contra > 10:
                tk.Label(container, text=f"... and {n_contra - 10} more",
                         font=self.font_small, bg=COLORS["bg"],
                         fg=COLORS["fg_dim"]).pack(anchor="w", pady=(4, 0))

        # -- Explanations section
        from coherence_engine.core.explanation import ExplanationGenerator
        explainer = ExplanationGenerator()
        explanations = explainer.explain(result)
        if explanations:
            tk.Frame(container, bg=COLORS["border"], height=1).pack(fill="x", pady=(16, 16))
            tk.Label(container, text="Explanations",
                     font=self.font_subheading, bg=COLORS["bg"],
                     fg=COLORS["fg_bright"]).pack(anchor="w", pady=(0, 8))

            for item in explanations[:15]:
                tk.Label(container, text=f"  • {item}",
                         font=self.font_small, bg=COLORS["bg"],
                         fg=COLORS["fg"], anchor="w", wraplength=750,
                         justify="left").pack(anchor="w", pady=1)

    def _display_delegation_results(self, delegated):
        self.notebook.select(1)
        self.status_label.config(text="Parallel delegation complete")

        for w in self.results_container.winfo_children():
            w.destroy()

        container = self.results_container
        decision = delegated.get("delegation", {})

        header = tk.Frame(container, bg=COLORS["bg"])
        header.pack(fill="x", pady=(0, 12))
        agg = float(delegated.get("aggregate_score", 0.0))
        tk.Label(header, text=f"{agg:.2f}", font=self.font_score, bg=COLORS["bg"],
                 fg=self._score_color(agg)).pack(side="left")
        info = tk.Frame(header, bg=COLORS["bg"])
        info.pack(side="left", padx=(16, 0), anchor="s", pady=(0, 8))
        tk.Label(info, text="/ 1.00  Aggregate Delegation Score",
                 font=self.font_body, bg=COLORS["bg"], fg=COLORS["fg_dim"]).pack(anchor="w")
        tk.Label(info, text=f"Reason: {decision.get('reason', 'unknown')}",
                 font=self.font_small, bg=COLORS["bg"], fg=COLORS["accent"]).pack(anchor="w")
        tk.Label(info, text=f"Parallel agents used: {delegated.get('parallel_agents_used', 1)}",
                 font=self.font_small, bg=COLORS["bg"], fg=COLORS["fg"]).pack(anchor="w")

        tk.Frame(container, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 14))
        tk.Label(container, text="Delegate Runs", font=self.font_subheading,
                 bg=COLORS["bg"], fg=COLORS["fg_bright"]).pack(anchor="w", pady=(0, 8))

        for run in delegated.get("runs", []):
            card = tk.Frame(container, bg=COLORS["bg_secondary"],
                            highlightthickness=1,
                            highlightbackground=COLORS["border"])
            card.pack(fill="x", pady=4)
            agent_name = run.get("agent", {}).get("name", "unknown")
            chunk_idx = run.get("chunk_index", "?")
            words = run.get("chunk_word_count", 0)
            score = float(run.get("score", 0.0))
            tk.Label(card,
                     text=f"Chunk {chunk_idx}  |  Agent: {agent_name}  |  Words: {words}  |  Score: {score:.3f}",
                     font=self.font_body_bold,
                     bg=COLORS["bg_secondary"],
                     fg=self._score_color(score),
                     anchor="w").pack(fill="x", padx=12, pady=(10, 4))
            prompt_preview = run.get("delegate_prompt", "")[:260]
            tk.Label(card,
                     text=f"{prompt_preview}...",
                     font=self.font_small,
                     bg=COLORS["bg_secondary"],
                     fg=COLORS["fg_dim"],
                     justify="left",
                     anchor="w",
                     wraplength=760).pack(fill="x", padx=12, pady=(0, 10))

        tk.Frame(container, bg=COLORS["border"], height=1).pack(fill="x", pady=(14, 14))
        tk.Label(container, text="Synthesis Prompt", font=self.font_subheading,
                 bg=COLORS["bg"], fg=COLORS["fg_bright"]).pack(anchor="w", pady=(0, 8))
        tk.Label(container,
                 text=delegated.get("synthesis_prompt", ""),
                 font=self.font_small,
                 bg=COLORS["bg"],
                 fg=COLORS["fg"],
                 justify="left",
                 anchor="w",
                 wraplength=760).pack(fill="x")
        self._populate_delegation_tab(delegated)
        self.notebook.select(self.delegation_tab)

    def _populate_delegation_tab(self, delegated):
        decision = delegated.get("delegation", {})
        self.delegation_status_label.config(
            text=(
                f"Delegated={decision.get('delegated')}  |  "
                f"reason={decision.get('reason')}  |  "
                f"agents={delegated.get('parallel_agents_used', 1)}  |  "
                f"aggregate={delegated.get('aggregate_score', 0.0)}"
            )
        )

        blocks = []
        for run in delegated.get("runs", []):
            agent = run.get("agent", {}).get("name", "unknown")
            idx = run.get("chunk_index", "?")
            words = run.get("chunk_word_count", 0)
            score = run.get("score", 0.0)
            report = run.get("report", "")
            delegate_prompt = run.get("delegate_prompt", "")
            blocks.append(
                "\n".join(
                    [
                        "=" * 80,
                        f"CHUNK {idx} | AGENT {agent} | WORDS {words} | SCORE {score}",
                        "=" * 80,
                        "",
                        "Delegate Prompt:",
                        delegate_prompt,
                        "",
                        "Full Chunk Report:",
                        report,
                        "",
                    ]
                )
            )

        full_text = "\n".join(blocks) if blocks else "No chunk reports available."
        self.delegation_text.config(state="normal")
        self.delegation_text.delete("1.0", "end")
        self.delegation_text.insert("1.0", full_text)
        self.delegation_text.config(state="disabled")

        synthesis = delegated.get("synthesis_prompt", "")
        self.synthesis_text.config(state="normal")
        self.synthesis_text.delete("1.0", "end")
        self.synthesis_text.insert("1.0", synthesis or "No synthesis prompt available.")
        self.synthesis_text.config(state="disabled")

    def _draw_layer_bar(self, parent, layer_result):
        """Draw a single layer bar with label, bar, and score."""
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=3)

        name_map = {
            "contradiction": "Contradiction",
            "argumentation": "Argumentation",
            "embedding": "Embedding",
            "compression": "Compression",
            "structural": "Structural",
        }
        display_name = name_map.get(layer_result.name, layer_result.name.title())

        tk.Label(row, text=display_name, font=self.font_body,
                 bg=COLORS["bg"], fg=COLORS["fg"],
                 width=15, anchor="w").pack(side="left")

        bar_frame = tk.Frame(row, bg=COLORS["bar_empty"], height=20)
        bar_frame.pack(side="left", fill="x", expand=True, padx=(8, 8))
        bar_frame.pack_propagate(False)

        fill_width = max(0.0, min(1.0, layer_result.score))
        fill_color = self._score_color(layer_result.score)

        fill_bar = tk.Frame(bar_frame, bg=fill_color, height=20)
        fill_bar.place(relx=0, rely=0, relwidth=fill_width, relheight=1.0)

        score_text = f"{layer_result.score:.2f}"
        tk.Label(row, text=score_text, font=self.font_mono,
                 bg=COLORS["bg"], fg=self._score_color(layer_result.score),
                 width=5).pack(side="left")

        weight_text = f"×{layer_result.weight:.2f}"
        tk.Label(row, text=weight_text, font=self.font_mono_small,
                 bg=COLORS["bg"], fg=COLORS["fg_dim"],
                 width=6).pack(side="left")

    # -- Helpers --------------------------------------------------------------

    def _score_color(self, score):
        if score >= 0.7:
            return COLORS["green"]
        elif score >= 0.4:
            return COLORS["yellow"]
        else:
            return COLORS["red"]

    def _interpret(self, score):
        if score > 0.8:
            return "Highly Coherent"
        elif score > 0.6:
            return "Coherent"
        elif score > 0.4:
            return "Moderate"
        elif score > 0.2:
            return "Weak"
        return "Incoherent"

    def _export_result(self, fmt):
        if not self._result and not self._delegation_result:
            return
        if self._delegation_result is not None:
            if fmt == "json":
                text = json.dumps(self._delegation_result, indent=2)
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.status_label.config(text="Delegation JSON copied to clipboard")
                return
            if fmt == "text":
                text = self._delegation_text_report()
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                self.status_label.config(text="Delegation report copied to clipboard")
                return
            if fmt == "save":
                path = filedialog.asksaveasfilename(
                    title="Save Delegation Report",
                    defaultextension=".txt",
                    filetypes=[("Text", "*.txt"), ("JSON", "*.json"), ("Markdown", "*.md")],
                )
                if path:
                    ext = os.path.splitext(path)[1].lower()
                    if ext == ".json":
                        report = json.dumps(self._delegation_result, indent=2)
                    else:
                        report = self._delegation_text_report()
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(report)
                    self.status_label.config(text=f"Saved: {os.path.basename(path)}")
                return
        if fmt == "json":
            text = self._result.report(fmt="json")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_label.config(text="JSON copied to clipboard")
        elif fmt == "text":
            text = self._result.report(fmt="text")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_label.config(text="Report copied to clipboard")
        elif fmt == "save":
            path = filedialog.asksaveasfilename(
                title="Save Report",
                defaultextension=".txt",
                filetypes=[("Text", "*.txt"), ("JSON", "*.json"), ("Markdown", "*.md")],
            )
            if path:
                ext = os.path.splitext(path)[1].lower()
                fmt_map = {".json": "json", ".md": "markdown"}
                report_fmt = fmt_map.get(ext, "text")
                report = self._result.report(fmt=report_fmt)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(report)
                self.status_label.config(text=f"Saved: {os.path.basename(path)}")

    def _delegation_text_report(self):
        d = self._delegation_result or {}
        decision = d.get("delegation", {})
        lines = [
            "PARALLEL PROMPT DELEGATION",
            "=" * 60,
            f"Delegated: {decision.get('delegated')}",
            f"Reason: {decision.get('reason')}",
            f"Parallel agents used: {d.get('parallel_agents_used')}",
            f"Aggregate score: {d.get('aggregate_score')}",
            "-" * 60,
        ]
        for run in d.get("runs", []):
            lines.append(
                "Chunk {idx} | agent={agent} | words={words} | score={score}".format(
                    idx=run.get("chunk_index"),
                    agent=run.get("agent", {}).get("name", "unknown"),
                    words=run.get("chunk_word_count"),
                    score=run.get("score"),
                )
            )
        lines.extend([
            "-" * 60,
            "Synthesis Prompt",
            "-" * 60,
            d.get("synthesis_prompt", ""),
        ])
        return "\n".join(lines)

    def _copy_synthesis_prompt(self):
        synthesis = ""
        if self._delegation_result is not None:
            synthesis = self._delegation_result.get("synthesis_prompt", "")
        if not synthesis:
            self.status_label.config(text="No synthesis prompt to copy")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(synthesis)
        self.status_label.config(text="Synthesis prompt copied to clipboard")

    def _bind_shortcuts(self):
        self.root.bind("<Command-o>", lambda e: self._open_file())
        self.root.bind("<Command-Return>", lambda e: self._run_analysis())
        self.root.bind("<Command-v>", lambda e: self._paste_clipboard())

    # -- Run ------------------------------------------------------------------

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = CoherenceEngineApp()
    app.run()


if __name__ == "__main__":
    main()
