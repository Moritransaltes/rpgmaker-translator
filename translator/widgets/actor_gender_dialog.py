"""Dialog for manually assigning gender to actors for pronoun accuracy."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QComboBox, QLabel, QHeaderView, QAbstractItemView,
)
from PyQt6.QtCore import Qt


GENDER_OPTIONS = ["unknown", "female", "male"]


class ActorGenderDialog(QDialog):
    """Shows all actors and lets the user assign genders for correct pronouns."""

    def __init__(self, actors: list, parent=None, translations: dict | None = None):
        """
        Args:
            actors: List of dicts with keys: id, name, nickname, profile, auto_gender
            translations: Optional {actor_id: {"name": str, "nickname": str, "profile": str}}
        """
        super().__init__(parent)
        self.actors = actors
        self._translations = translations or {}
        self.setWindowTitle("Assign Character Genders")
        self.setMinimumSize(700, 400)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "Assign genders so the translator uses correct pronouns (he/she).\n"
            "Auto-detected genders are pre-filled but may be wrong — please verify."
        ))

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Name", "Nickname", "Profile", "Gender"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 40)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 120)

        self.table.setRowCount(len(self.actors))
        self._combos = []

        for row, actor in enumerate(self.actors):
            aid = actor.get("id", 0)
            tl = self._translations.get(aid, {})

            # ID
            id_item = QTableWidgetItem(str(aid))
            id_item.setFlags(id_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, id_item)

            # Name — show "JP → EN" when translation is available
            jp_name = actor.get("name", "")
            en_name = tl.get("name", "")
            name_display = f"{jp_name} \u2192 {en_name}" if en_name and en_name != jp_name else jp_name
            name_item = QTableWidgetItem(name_display)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(jp_name)
            self.table.setItem(row, 1, name_item)

            # Nickname
            jp_nick = actor.get("nickname", "")
            en_nick = tl.get("nickname", "")
            nick_display = f"{jp_nick} \u2192 {en_nick}" if en_nick and en_nick != jp_nick else jp_nick
            nick_item = QTableWidgetItem(nick_display)
            nick_item.setFlags(nick_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            nick_item.setToolTip(jp_nick)
            self.table.setItem(row, 2, nick_item)

            # Profile — show translated version when available, JP in tooltip
            jp_profile = actor.get("profile", "")
            en_profile = tl.get("profile", "")
            profile = en_profile if en_profile else jp_profile
            profile_display = profile[:80] + "..." if len(profile) > 80 else profile
            prof_item = QTableWidgetItem(profile_display)
            prof_item.setFlags(prof_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            prof_item.setToolTip(jp_profile if en_profile else profile)
            self.table.setItem(row, 3, prof_item)

            # Gender combo
            combo = QComboBox()
            combo.addItems(GENDER_OPTIONS)
            auto = actor.get("auto_gender", "")
            if auto in GENDER_OPTIONS:
                combo.setCurrentText(auto)
            self.table.setCellWidget(row, 4, combo)
            self._combos.append(combo)

        layout.addWidget(self.table)

        # Buttons
        btn_row = QHBoxLayout()

        all_female_btn = QPushButton("All Female")
        all_female_btn.clicked.connect(lambda: self._set_all("female"))
        btn_row.addWidget(all_female_btn)

        all_male_btn = QPushButton("All Male")
        all_male_btn.clicked.connect(lambda: self._set_all("male"))
        btn_row.addWidget(all_male_btn)

        btn_row.addStretch()

        ok_btn = QPushButton("Apply")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)

        skip_btn = QPushButton("Skip (use auto-detect)")
        skip_btn.clicked.connect(self.reject)
        btn_row.addWidget(skip_btn)

        layout.addLayout(btn_row)

    def _set_all(self, gender: str):
        """Set all combos to the same gender."""
        for combo in self._combos:
            combo.setCurrentText(gender)

    def get_genders(self) -> dict:
        """Return {actor_id: gender} mapping."""
        result = {}
        for row, actor in enumerate(self.actors):
            gender = self._combos[row].currentText()
            if gender != "unknown":
                result[actor["id"]] = gender
        return result
