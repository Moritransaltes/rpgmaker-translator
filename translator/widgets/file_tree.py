"""File tree widget showing project files grouped by type."""

from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem
from PyQt6.QtCore import pyqtSignal, Qt

from ..project_model import TranslationProject


# File categories for grouping
FILE_CATEGORIES = {
    "Database": [
        "Actors.json", "Classes.json", "Items.json", "Weapons.json",
        "Armors.json", "Skills.json", "States.json", "Enemies.json",
    ],
    "System": ["System.json"],
    "Common Events": ["CommonEvents.json"],
}


class FileTreeWidget(QTreeWidget):
    """Tree view showing project files organized by category."""

    file_selected = pyqtSignal(str)   # Emits filename when clicked
    all_selected = pyqtSignal()       # Emits when "All Files" is clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabels(["File", "Progress"])
        self.setColumnWidth(0, 200)
        self.setMinimumWidth(250)
        self.itemClicked.connect(self._on_item_clicked)

    def load_project(self, project: TranslationProject):
        """Populate tree from a loaded translation project."""
        self.setUpdatesEnabled(False)
        self.clear()

        # "All Files" root item
        all_item = QTreeWidgetItem(self, ["All Files", f"{project.translated_count}/{project.total}"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, "__ALL__")
        all_item.setExpanded(True)

        files = project.get_files()
        categorized = set()

        # Add categorized files
        for category, file_list in FILE_CATEGORIES.items():
            cat_files = [f for f in file_list if f in files]
            if not cat_files:
                continue

            cat_translated = 0
            cat_total = 0
            cat_item = QTreeWidgetItem(all_item, [category, ""])

            for filename in cat_files:
                translated, total = project.stats_for_file(filename)
                cat_translated += translated
                cat_total += total
                file_item = QTreeWidgetItem(cat_item, [filename, f"{translated}/{total}"])
                file_item.setData(0, Qt.ItemDataRole.UserRole, filename)
                categorized.add(filename)

            cat_item.setText(1, f"{cat_translated}/{cat_total}")
            cat_item.setExpanded(True)

        # Maps category — any Map###.json files
        map_files = sorted([f for f in files if f.startswith("Map") and f not in categorized])
        if map_files:
            map_translated = 0
            map_total = 0
            maps_item = QTreeWidgetItem(all_item, ["Maps", ""])

            for filename in map_files:
                translated, total = project.stats_for_file(filename)
                map_translated += translated
                map_total += total
                file_item = QTreeWidgetItem(maps_item, [filename, f"{translated}/{total}"])
                file_item.setData(0, Qt.ItemDataRole.UserRole, filename)
                categorized.add(filename)

            maps_item.setText(1, f"{map_translated}/{map_total}")
            maps_item.setExpanded(False)

        # Any remaining uncategorized files
        remaining = [f for f in files if f not in categorized]
        if remaining:
            other_item = QTreeWidgetItem(all_item, ["Other", ""])
            for filename in remaining:
                translated, total = project.stats_for_file(filename)
                file_item = QTreeWidgetItem(other_item, [filename, f"{translated}/{total}"])
                file_item.setData(0, Qt.ItemDataRole.UserRole, filename)

        self.setUpdatesEnabled(True)

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Handle click — emit the appropriate signal."""
        filename = item.data(0, Qt.ItemDataRole.UserRole)
        if filename == "__ALL__":
            self.all_selected.emit()
        elif filename:
            self.file_selected.emit(filename)

    def refresh_stats(self, project: TranslationProject):
        """Update progress counts without rebuilding the tree."""
        self._update_item_stats(self.invisibleRootItem(), project)

    def _update_item_stats(self, item, project: TranslationProject) -> tuple:
        """Recursively update stat labels. Returns (translated, total) sum for children."""
        cat_translated = 0
        cat_total = 0
        for i in range(item.childCount()):
            child = item.child(i)
            filename = child.data(0, Qt.ItemDataRole.UserRole)
            if filename == "__ALL__":
                child.setText(1, f"{project.translated_count}/{project.total}")
                self._update_item_stats(child, project)
            elif filename:
                translated, total = project.stats_for_file(filename)
                child.setText(1, f"{translated}/{total}")
                cat_translated += translated
                cat_total += total
                self._update_item_stats(child, project)
            else:
                # Category node — recurse and sum up children
                sub_t, sub_total = self._update_item_stats(child, project)
                child.setText(1, f"{sub_t}/{sub_total}")
                cat_translated += sub_t
                cat_total += sub_total
        return cat_translated, cat_total
