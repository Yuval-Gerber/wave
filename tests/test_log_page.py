"""Phase 8.9: the Log tab — virtualized model, filters, export, delete."""

import sqlite3

import pytest

from waveapp.persistence.db import Database
from waveapp.ui.log_page import _BATCH, LogPage


@pytest.fixture
def log_db(tmp_path):
    database = Database(tmp_path / "test_log.db")
    for index in range(1000):
        category = ("TRADE", "SCANNER", "SYSTEM", "RISK")[index % 4]
        level = "ERROR" if index % 100 == 0 else "INFO"
        database.execute(
            "INSERT INTO log (ts, category, level, message) VALUES (?,?,?,?)",
            (
                f"2026-08-{10 + index % 5:02d}T12:{index % 60:02d}:00+00:00",
                category,
                level,
                f"message number {index} spread check",
            ),
        )
    yield database
    database.close()


def _page(qtbot, database) -> LogPage:
    page = LogPage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.attach_database(database)
    return page


def test_lazy_loading_keeps_memory_small(qtbot, log_db):
    page = _page(qtbot, log_db)
    assert page.model.total == 1000
    assert page.model.rowCount() == 0  # nothing loaded yet
    page.model.fetchMore()
    assert page.model.rowCount() == _BATCH  # one batch, not the world
    assert page.model.canFetchMore()
    assert "1,000 entries" in page.count_label.text()


def test_search_and_category_filters(qtbot, log_db):
    page = _page(qtbot, log_db)
    page.search.setText("number 42 spread")
    page._apply_filters()  # bypass the debounce in tests
    assert page.model.total == 1  # fragment match
    page.search.clear()
    page.category.setCurrentText("TRADE")
    page._apply_filters()
    assert page.model.total == 250
    page.category.setCurrentText("All")
    page._apply_filters()
    assert page.model.total == 1000


def test_date_filter(qtbot, log_db):
    from datetime import date

    page = _page(qtbot, log_db)
    page.from_button.selected = date(2026, 8, 12)
    page.to_button.selected = date(2026, 8, 12)
    page._apply_filters()
    assert page.model.total == 200  # one of the five days


def test_export_selected_and_filtered(qtbot, log_db, tmp_path):
    page = _page(qtbot, log_db)
    page.category.setCurrentText("RISK")
    page._apply_filters()
    out = tmp_path / "export.sqlite"
    exported = page.export_to(str(out))  # no selection → whole filter
    assert exported == 250
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM log").fetchone()[0] == 250
    assert conn.execute("SELECT COUNT(*) FROM log WHERE category != 'RISK'").fetchone()[0] == 0
    conn.close()

    # explicit CHECKED rows export only those rows (r2: checkboxes)
    from PyQt6.QtCore import Qt

    page.model.fetchMore()
    for row in (0, 1):
        page.model.setData(
            page.model.index(row, 0), Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole
        )
    out2 = tmp_path / "export2.sqlite"
    assert page.export_to(str(out2)) == 2


def test_delete_checked_and_whole_filter(qtbot, log_db):
    from PyQt6.QtCore import Qt

    page = _page(qtbot, log_db)
    page.model.fetchMore()
    page.model.setData(
        page.model.index(0, 0), Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole
    )
    deleted = page.delete_selected()
    assert deleted == 1
    assert page.model.total == 999

    # "Select all filtered" spans the WHOLE filter (not just loaded rows)
    page.model.fetchMore()  # 400 of 999 loaded
    page.model.check_all()
    assert page.model.whole_filter_selected()
    deleted = page.delete_selected()
    assert deleted == 999
    assert page.model.total == 0


def test_select_all_button_toggles_and_unchecks(qtbot, log_db):
    """r2: the button checks every filtered row, then flips to
    'Unselect all filtered' and clears them; unchecking one row while
    all-selected excludes it from delete/export."""
    from PyQt6.QtCore import Qt

    page = _page(qtbot, log_db)
    page.model.fetchMore()
    assert page.select_all_button.text() == "Select all filtered"
    page._toggle_select_all()
    assert page.model.selection_count() == 1000
    assert page.select_all_button.text() == "Unselect all filtered"
    assert page.model.data(page.model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == (
        Qt.CheckState.Checked
    )
    # uncheck one row → it survives a delete of "all filtered"
    page.model.setData(
        page.model.index(0, 0), Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole
    )
    assert page.model.selection_count() == 999
    assert page.select_all_button.text() == "Select all filtered"  # no longer whole
    deleted = page.delete_selected()
    assert deleted == 999
    assert page.model.total == 1  # the unchecked survivor
    page._toggle_select_all()
    page._toggle_select_all()  # select-all then unselect-all
    assert page.model.selection_count() == 0


def test_double_click_opens_entry_popup(qtbot, log_db):
    page = _page(qtbot, log_db)
    page.model.fetchMore()
    page._open_entry(page.model.index(0, 4))
    popup = page._entry_popup
    assert popup is not None
    texts = [label.text() for label in popup.card.findChildren(type(page.count_label))]
    assert any("message number" in text for text in texts)


def test_no_row_highlight(qtbot, log_db):
    from PyQt6.QtWidgets import QTableView

    page = _page(qtbot, log_db)
    assert page.table.selectionMode() == QTableView.SelectionMode.NoSelection
