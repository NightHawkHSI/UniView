"""Options > Memory settings: UniView's allocated RAM, when to warn, and when to slow background work down."""

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QLabel, QSpinBox,
                               QVBoxLayout)

from uniview.perf import fmt_gb, ram_budget

DEFAULTS = {"ram_budget_gb": 0.0, "ram_warn_pct": 90, "ram_slow_pct": 85}


class MemoryDialog(QDialog):
    def __init__(self, settings, stats, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Memory settings")
        self.settings = settings
        self.total = stats.get("sys_ram_total", 0)
        total_gb = self.total / 2**30 if self.total else 64.0

        now = stats.get("app_private")
        intro = QLabel(
            (f"Installed RAM: <b>{fmt_gb(self.total)}</b>" if self.total else "Installed RAM: unknown")
            + (f" &nbsp;·&nbsp; UniView is using <b>{fmt_gb(now)}</b>" if now else ""))
        help_text = QLabel(
            "<b>Allocated RAM</b> is how much memory UniView aims to stay within. Going over it frees cached "
            "thumbnails and slows background work right down.<br>"
            "<b>Warn at</b> shows a warning once UniView uses that share of its allocation.<br>"
            "<b>Slow down at</b> eases off background work (thumbnails, sort/search stats) once the computer's "
            "memory, or UniView's share of its allocation, gets that full - so the window keeps responding.<br>"
            "Big games still load if they need more; these settings decide when UniView warns and backs off.")
        help_text.setWordWrap(True)
        help_text.setProperty("role", "muted")

        self.budget = QDoubleSpinBox()
        self.budget.setRange(0.0, max(1.0, round(total_gb, 1)))
        self.budget.setSingleStep(0.5)
        self.budget.setDecimals(1)
        self.budget.setSuffix(" GB")
        self.budget.setSpecialValueText(f"Auto ({fmt_gb(ram_budget(0, self.total))})")
        self.budget.setValue(settings.ram_budget_gb)
        self.budget.setToolTip("0 = Auto: half the installed RAM, at least 4 GB.")

        self.warn = QSpinBox()
        self.warn.setRange(50, 100)
        self.warn.setSuffix(" % of allocated RAM")
        self.warn.setValue(settings.ram_warn_pct)

        self.slow = QSpinBox()
        self.slow.setRange(50, 98)
        self.slow.setSuffix(" % memory used")
        self.slow.setValue(settings.ram_slow_pct)

        form = QFormLayout()
        form.addRow("Allocated RAM:", self.budget)
        form.addRow("Max RAM warning at:", self.warn)
        form.addRow("Slow down background work at:", self.slow)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self.restore_defaults)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(help_text)
        layout.addWidget(buttons)
        self.resize(520, self.sizeHint().height())

    def restore_defaults(self):
        self.budget.setValue(DEFAULTS["ram_budget_gb"])
        self.warn.setValue(DEFAULTS["ram_warn_pct"])
        self.slow.setValue(DEFAULTS["ram_slow_pct"])

    def apply(self):
        """Copy the values into the settings (call after exec() returns Accepted)."""
        self.settings.ram_budget_gb = round(self.budget.value(), 1)
        self.settings.ram_warn_pct = self.warn.value()
        self.settings.ram_slow_pct = self.slow.value()
