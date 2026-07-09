import os

import pcbnew

from .ui.chat_window import ChatWindow
from .ui.settings import PluginSettings

class AIReviewerPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Maelectrix"
        self.category = "Inspection"
        self.description = "Open AI assistant chat for on-demand project checks"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "images", "icon-m8.png")
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        board = pcbnew.GetBoard()
        settings = PluginSettings()
        context_dir = settings.context_dir(os.path.dirname(board.GetFileName()))
        os.makedirs(context_dir, exist_ok=True)
        ChatWindow.show(board.GetFileName(), context_dir)

AIReviewerPlugin().register()
