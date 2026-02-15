"""RPG Maker Translator — Local LLM Translation Tool.

Launch with: python main.py
"""

import sys
from PyQt6.QtWidgets import QApplication
from translator.widgets.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("RPG Maker Translator")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

