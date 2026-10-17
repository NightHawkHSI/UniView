"""The welcome window shown at startup: what UniView reads by itself, what plugins are and how to install them.
"Don't show this at startup" turns it off (Settings.show_welcome); Help -> Welcome / plugins guide shows it again.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout

from uniview import __version__
from uniview.constants import APP_SHORT, PLUGINS_REPO_URL, REPO_URL
from uniview.ui.theme import role

GUIDE = f"""
<h3>What {APP_SHORT} opens by itself</h3>
<p><b>Unity</b> games and <b>loose files</b> (images, sounds, text, <code>.obj</code> models and zip-style archives
of any game) work straight away.</p>

<h3>Everything else: engine plugins</h3>
<p>Other engines and games - <b>Source</b> (Team Fortress 2, Portal, Garry's Mod...), <b>Source 2</b> (Counter-Strike 2,
Deadlock, Dota 2), <b>Unreal</b> 4/5, <b>Terraria</b>, <b>Spiral Knights</b>, <b>Wakfu</b>, <b>Spore</b>,
<b>Fallout 1/2</b>, <b>Fallout Tactics</b>, <b>Risk of Rain</b>, <b>Serious Sam 2</b> - are read by free
<b>plugins</b>. A plugin is a small Python file that teaches {APP_SHORT} one engine.</p>

<h3>How to install a plugin</h3>
<ol>
<li><b>Easiest:</b> click <b>Get Engines / Plugins</b> (top right of the window, or the <b>Plugins</b> tab on the start
page), pick the plugin for your game and click <b>Install</b>. {APP_SHORT} downloads it, checks it, and asks you to
restart.</li>
<li><b>By hand:</b> download the plugin's <code>.py</code> file (and the helper folder it needs, e.g.
<code>valve_formats/</code> for Source) from <a href="{PLUGINS_REPO_URL}">UniView-Plugins</a> and put it in the
<code>plugins</code> folder next to {APP_SHORT}. Restart.</li>
</ol>
<p>When you add a game that needs a plugin you don't have, {APP_SHORT} tells you (<i>Plugin available</i>) and takes
you to it. <b>Options &rarr; Plugin manager</b> switches plugins off or removes them.</p>

<h3>Good to know</h3>
<ul>
<li>Plugins are <b>third-party code</b> that runs on your computer. The ones in the Plugins tab are open source and
only read game files, but only install plugins you trust.</li>
<li>Plugins live in their own repository and are <b>not affiliated with any game developer or publisher</b>.
Check that a game's license allows extracting its files, and don't share extracted assets.</li>
<li>Want to read a new engine? Copy <code>plugins/_template.py</code> and see
<a href="{REPO_URL}/blob/main/PLUGINS.md">PLUGINS.md</a>.</li>
</ul>
"""


class WelcomeDialog(QDialog):
    """Welcome + plugin guide. After exec(), `open_plugins` says whether to show the Plugins tab."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.open_plugins = False
        self.setWindowTitle(f"Welcome to {APP_SHORT}")
        self.resize(680, 600)

        title = role(QLabel(f"Welcome to {APP_SHORT} {__version__}"), "title")
        text = QTextBrowser()
        text.setOpenExternalLinks(True)
        text.setHtml(GUIDE)
        self.dont_show = QCheckBox("Don't show this at startup")
        self.dont_show.setChecked(not settings.show_welcome)
        self.dont_show.setToolTip("Help → Welcome and plugin guide shows it again")

        get = role(QPushButton("⬇  Get Engines / Plugins"), "primary")
        get.setDefault(True)
        get.clicked.connect(self._get_plugins)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.dont_show)
        row.addStretch(1)
        row.addWidget(get)
        row.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(text, 1)
        lay.addLayout(row)

    def _get_plugins(self):
        self.open_plugins = True
        self.accept()

    def done(self, result):
        show = not self.dont_show.isChecked()
        if show != self.settings.show_welcome:
            self.settings.show_welcome = show
            self.settings.save()
        super().done(result)


def show_welcome(settings, parent=None):
    """Show the window; True if the user asked for the Plugins tab."""
    dialog = WelcomeDialog(settings, parent)
    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.exec()
    return dialog.open_plugins
