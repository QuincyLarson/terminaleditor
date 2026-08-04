#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Author: quincylarson
# Manual install (macOS/Linux):
# python3 -c "import curses"
# mkdir -p ~/.local/bin
# install -m 755 ~/Downloads/terminaleditor.py ~/.local/bin/terminaleditor
# ln -sf terminaleditor ~/.local/bin/te
# echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc  # use ~/.bashrc for bash
# exec "$SHELL" -l
# te currentfilename.txt
# Homebrew and apt packages install both terminaleditor and te automatically.

from __future__ import annotations

import argparse
import curses
import os
import stat
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "TerminalEditor"
VERSION = "0.1.0"
AUTOSAVE_SECONDS = 0.5
UNDO_LIMIT = 200
TAB_WIDTH = 4
EDIT_WIDTH = 80
SHORTCUT_TEXT = """^Q quit
^Z undo    ^R redo
^A line start    ^E line end
^B back    ^F forward    ^P up    ^N down
^H backspace    ^D delete
^K cut to line end    ^Y yank
^W delete word    ^U delete to line start
^T transpose
^[ paragraph up    ^] paragraph down
Arrows Home End Backspace Delete Enter Tab also work
^/ paste shortcuts"""


def character_width(character: str, column: int) -> int:
    if character == "\t":
        return TAB_WIDTH - (column % TAB_WIDTH)
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character).startswith("C"):
        return 1
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def displayed_text(text: str) -> str:
    result: list[str] = []
    column = 0
    for character in text:
        if character == "\t":
            width = character_width(character, column)
            result.append(" " * width)
            column += width
        elif unicodedata.category(character).startswith("C"):
            result.append("?")
            column += 1
        else:
            result.append(character)
            column += character_width(character, column)
    return "".join(result)


@dataclass(frozen=True)
class VisualRow:
    line: int
    start: int
    end: int


@dataclass(frozen=True)
class Snapshot:
    lines: tuple[str, ...]
    line: int
    column: int


def wrap_line(line_number: int, text: str, width: int) -> list[VisualRow]:
    width = max(1, width)
    if not text:
        return [VisualRow(line_number, 0, 0)]

    rows: list[VisualRow] = []
    start = 0
    while start < len(text):
        index = start
        cells = 0
        word_break: int | None = None

        while index < len(text):
            size = character_width(text[index], cells)
            if cells and cells + size > width:
                end = word_break if word_break is not None else index
                rows.append(VisualRow(line_number, start, end))
                start = end
                break

            if not cells and size > width:
                rows.append(VisualRow(line_number, start, index + 1))
                start = index + 1
                break

            cells += size
            index += 1
            if text[index - 1].isspace():
                word_break = index

            if cells == width:
                if index == len(text):
                    rows.append(VisualRow(line_number, start, index))
                    start = index
                else:
                    end = word_break if word_break is not None else index
                    rows.append(VisualRow(line_number, start, end))
                    start = end
                break
        else:
            rows.append(VisualRow(line_number, start, len(text)))
            start = len(text)
    return rows


class Editor:
    def __init__(self, path: Path, text: str, newline: str, is_new: bool) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self.path = path
        self.newline = newline
        self.lines = normalized.split("\n")
        self.line = 0
        self.column = 0
        self.preferred_screen_column: int | None = None
        self.top_visual_row = 0
        self.cut_buffer: str | None = None

        self.undo_stack: list[Snapshot] = []
        self.redo_stack: list[Snapshot] = []
        self.last_edit_kind: str | None = None
        self.last_edit_at = 0.0

        self.dirty = is_new
        self.last_save_attempt = time.monotonic()
        self.save_error: str | None = None

    def snapshot(self) -> Snapshot:
        return Snapshot(tuple(self.lines), self.line, self.column)

    def restore(self, snapshot: Snapshot) -> None:
        self.lines = list(snapshot.lines)
        self.line = snapshot.line
        self.column = snapshot.column
        self.preferred_screen_column = None
        self.top_visual_row = 0

    def before_edit(self, kind: str, *, coalesce: bool = False) -> None:
        now = time.monotonic()
        continuing = (
            coalesce
            and self.last_edit_kind == kind
            and now - self.last_edit_at <= AUTOSAVE_SECONDS
        )
        if not continuing:
            self.undo_stack.append(self.snapshot())
            if len(self.undo_stack) > UNDO_LIMIT:
                del self.undo_stack[0]
        self.redo_stack.clear()
        self.last_edit_kind = kind if coalesce else None
        self.last_edit_at = now
        self.dirty = True
        self.save_error = None

    def stop_coalescing(self) -> None:
        self.last_edit_kind = None
        self.preferred_screen_column = None

    def undo(self) -> None:
        if not self.undo_stack:
            return
        self.redo_stack.append(self.snapshot())
        self.restore(self.undo_stack.pop())
        self.last_edit_kind = None
        self.dirty = True

    def redo(self) -> None:
        if not self.redo_stack:
            return
        self.undo_stack.append(self.snapshot())
        self.restore(self.redo_stack.pop())
        self.last_edit_kind = None
        self.dirty = True

    def insert(self, character: str) -> None:
        self.before_edit("insert", coalesce=True)
        current = self.lines[self.line]
        self.lines[self.line] = (
            current[: self.column] + character + current[self.column :]
        )
        self.column += len(character)
        self.preferred_screen_column = None

    def insert_newline(self) -> None:
        self.before_edit("newline")
        current = self.lines[self.line]
        self.lines[self.line] = current[: self.column]
        self.lines.insert(self.line + 1, current[self.column :])
        self.line += 1
        self.column = 0
        self.preferred_screen_column = None

    def insert_text(self, text: str) -> None:
        self.before_edit("paste")
        current = self.lines[self.line]
        start_column = self.column
        parts = text.split("\n")
        replacement = [current[: self.column] + parts[0]]
        replacement.extend(parts[1:-1])
        if len(parts) > 1:
            replacement.append(parts[-1] + current[self.column :])
        else:
            replacement[0] += current[self.column :]
        self.lines[self.line : self.line + 1] = replacement
        self.line += len(parts) - 1
        self.column = (
            len(parts[-1]) if len(parts) > 1 else start_column + len(text)
        )
        self.preferred_screen_column = None

    def move_left(self) -> None:
        self.stop_coalescing()
        if self.column:
            self.column -= 1
        elif self.line:
            self.line -= 1
            self.column = len(self.lines[self.line])

    def move_right(self) -> None:
        self.stop_coalescing()
        if self.column < len(self.lines[self.line]):
            self.column += 1
        elif self.line + 1 < len(self.lines):
            self.line += 1
            self.column = 0

    def backspace(self) -> None:
        if self.column:
            self.before_edit("backspace", coalesce=True)
            current = self.lines[self.line]
            self.lines[self.line] = (
                current[: self.column - 1] + current[self.column :]
            )
            self.column -= 1
        elif self.line:
            self.before_edit("backspace", coalesce=True)
            previous_length = len(self.lines[self.line - 1])
            self.lines[self.line - 1] += self.lines[self.line]
            del self.lines[self.line]
            self.line -= 1
            self.column = previous_length
        self.preferred_screen_column = None

    def delete(self) -> None:
        current = self.lines[self.line]
        if self.column < len(current):
            self.before_edit("delete", coalesce=True)
            self.lines[self.line] = current[: self.column] + current[self.column + 1 :]
        elif self.line + 1 < len(self.lines):
            self.before_edit("delete", coalesce=True)
            self.lines[self.line] += self.lines[self.line + 1]
            del self.lines[self.line + 1]
        self.preferred_screen_column = None

    def delete_previous_word(self) -> None:
        if not self.column:
            return
        start = self.column
        current = self.lines[self.line]
        while start and current[start - 1].isspace():
            start -= 1
        while start and not current[start - 1].isspace():
            start -= 1
        self.before_edit("word-delete", coalesce=True)
        self.lines[self.line] = current[:start] + current[self.column :]
        self.column = start
        self.preferred_screen_column = None

    def delete_to_line_start(self) -> None:
        if not self.column:
            return
        self.before_edit("line-start-delete")
        self.lines[self.line] = self.lines[self.line][self.column :]
        self.column = 0
        self.preferred_screen_column = None

    def transpose(self) -> None:
        current = self.lines[self.line]
        if len(current) < 2 or self.column == 0:
            return
        right = self.column if self.column < len(current) else self.column - 1
        left = right - 1
        self.before_edit("transpose")
        characters = list(current)
        characters[left], characters[right] = characters[right], characters[left]
        self.lines[self.line] = "".join(characters)
        self.column = min(len(current), right + 1)
        self.preferred_screen_column = None

    def cut_to_line_end(self) -> None:
        current = self.lines[self.line]
        if self.column == len(current):
            return
        self.before_edit("cut")
        self.cut_buffer = current[self.column :]
        self.lines[self.line] = current[: self.column]
        self.preferred_screen_column = None

    def yank(self) -> None:
        if self.cut_buffer is None:
            return
        self.before_edit("yank")
        current = self.lines[self.line]
        self.lines[self.line] = (
            current[: self.column] + self.cut_buffer + current[self.column :]
        )
        self.column += len(self.cut_buffer)
        self.preferred_screen_column = None

    def paragraph_bounds(self) -> tuple[int, int] | None:
        if not self.lines[self.line].strip():
            return None
        start = self.line
        end = self.line
        while start and self.lines[start - 1].strip():
            start -= 1
        while end + 1 < len(self.lines) and self.lines[end + 1].strip():
            end += 1
        return start, end

    def drag_paragraph(self, direction: int) -> None:
        bounds = self.paragraph_bounds()
        if bounds is None:
            return
        start, end = bounds
        offset = self.line - start

        if direction < 0:
            previous_end = start - 1
            while previous_end >= 0 and not self.lines[previous_end].strip():
                previous_end -= 1
            if previous_end < 0:
                return
            previous_start = previous_end
            while previous_start and self.lines[previous_start - 1].strip():
                previous_start -= 1

            self.before_edit("drag")
            previous = self.lines[previous_start : previous_end + 1]
            gap = self.lines[previous_end + 1 : start]
            current = self.lines[start : end + 1]
            self.lines[previous_start : end + 1] = current + gap + previous
            self.line = previous_start + offset
        else:
            next_start = end + 1
            while next_start < len(self.lines) and not self.lines[next_start].strip():
                next_start += 1
            if next_start == len(self.lines):
                return
            next_end = next_start
            while next_end + 1 < len(self.lines) and self.lines[next_end + 1].strip():
                next_end += 1

            self.before_edit("drag")
            current = self.lines[start : end + 1]
            gap = self.lines[end + 1 : next_start]
            following = self.lines[next_start : next_end + 1]
            self.lines[start : next_end + 1] = following + gap + current
            self.line = start + len(following) + len(gap) + offset

        self.column = min(self.column, len(self.lines[self.line]))
        self.preferred_screen_column = None

    def layout(self, width: int) -> list[VisualRow]:
        rows: list[VisualRow] = []
        for line_number, text in enumerate(self.lines):
            rows.extend(wrap_line(line_number, text, width))
        return rows

    def visual_position(
        self, rows: list[VisualRow], width: int
    ) -> tuple[int, int]:
        current_text = self.lines[self.line]
        for row_number, row in enumerate(rows):
            if row.line != self.line:
                continue
            at_line_end = self.column == row.end == len(current_text)
            if self.column < row.end or at_line_end or row.start == row.end:
                cells = 0
                for character in current_text[row.start : self.column]:
                    cells += character_width(character, cells)
                return row_number, min(cells, max(0, width - 1))
        return len(rows) - 1, 0

    def column_at_screen_x(self, row: VisualRow, target: int) -> int:
        text = self.lines[row.line]
        cells = 0
        for index in range(row.start, row.end):
            size = character_width(text[index], cells)
            if cells + size > target:
                return index
            cells += size
        return row.end

    def move_vertical(self, direction: int, width: int) -> None:
        self.last_edit_kind = None
        rows = self.layout(width)
        row_number, screen_column = self.visual_position(rows, width)
        if self.preferred_screen_column is None:
            self.preferred_screen_column = screen_column
        target = max(0, min(len(rows) - 1, row_number + direction))
        target_row = rows[target]
        self.line = target_row.line
        self.column = self.column_at_screen_x(
            target_row, self.preferred_screen_column
        )

    def save(self) -> None:
        data = self.newline.join(self.lines)
        parent = self.path.parent
        old_mode: int | None = None
        if self.path.exists():
            old_mode = stat.S_IMODE(self.path.stat().st_mode)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            if old_mode is None:
                current_umask = os.umask(0)
                os.umask(current_umask)
                old_mode = 0o666 & ~current_umask
            os.chmod(temporary, old_mode)
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        self.dirty = False
        self.save_error = None
        self.last_save_attempt = time.monotonic()

    def autosave_if_due(self) -> bool:
        now = time.monotonic()
        if not self.dirty or now - self.last_save_attempt < AUTOSAVE_SECONDS:
            return False
        had_error = self.save_error is not None
        self.last_save_attempt = now
        try:
            self.save()
        except OSError as error:
            self.save_error = str(error)
            return True
        return had_error

    def footer(self, width: int) -> str:
        if self.save_error:
            message = f"AUTOSAVE ERROR: {self.save_error}"
        else:
            message = f"{APP_NAME} {self.path.name} ^/ shortcuts"
        return message[:width].ljust(width)

    def draw(self, screen: curses.window) -> None:
        height, screen_width = screen.getmaxyx()
        screen.erase()
        if height < 2 or screen_width < 10:
            try:
                screen.addnstr(
                    0, 0, "Terminal too small", max(1, screen_width - 1)
                )
            except curses.error:
                pass
            screen.refresh()
            return

        content_height = height - 1
        width = min(EDIT_WIDTH, screen_width)
        left = (screen_width - width) // 2
        rows = self.layout(width)
        cursor_row, cursor_column = self.visual_position(rows, width)
        if cursor_row < self.top_visual_row:
            self.top_visual_row = cursor_row
        elif cursor_row >= self.top_visual_row + content_height:
            self.top_visual_row = cursor_row - content_height + 1
        self.top_visual_row = max(
            0, min(self.top_visual_row, max(0, len(rows) - content_height))
        )

        for screen_row in range(content_height):
            visual_row = self.top_visual_row + screen_row
            if visual_row >= len(rows):
                break
            row = rows[visual_row]
            text = displayed_text(self.lines[row.line][row.start : row.end])
            try:
                screen.addstr(screen_row, left, text)
            except curses.error:
                pass

        try:
            screen.addnstr(
                height - 1,
                left,
                self.footer(width),
                width if left + width < screen_width else max(1, width - 1),
                curses.A_REVERSE,
            )
        except curses.error:
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            screen.move(cursor_row - self.top_visual_row, left + cursor_column)
        except curses.error:
            pass
        screen.refresh()

    def handle_key(self, key: object, width: int) -> bool:
        if key == "\x11":  # Ctrl-Q
            if self.dirty:
                self.save()
            return False
        if key in ("\x1f", 31):  # Ctrl-/
            self.insert_text(SHORTCUT_TEXT)
            return True
        if key == "\x1a":  # Ctrl-Z; raw mode prevents terminal suspension.
            self.undo()
        elif key == "\x12":  # Ctrl-R
            self.redo()
        elif key in ("\x02", curses.KEY_LEFT):  # Ctrl-B
            self.move_left()
        elif key in ("\x06", curses.KEY_RIGHT):  # Ctrl-F
            self.move_right()
        elif key in ("\x10", curses.KEY_UP):  # Ctrl-P
            self.move_vertical(-1, width)
        elif key in ("\x0e", curses.KEY_DOWN):  # Ctrl-N
            self.move_vertical(1, width)
        elif key in ("\x01", curses.KEY_HOME):  # Ctrl-A
            self.stop_coalescing()
            self.column = 0
        elif key in ("\x05", curses.KEY_END):  # Ctrl-E
            self.stop_coalescing()
            self.column = len(self.lines[self.line])
        elif key in ("\x08", "\x7f", curses.KEY_BACKSPACE):  # Ctrl-H
            self.backspace()
        elif key in ("\x04", curses.KEY_DC):  # Ctrl-D
            self.delete()
        elif key == "\x0b":  # Ctrl-K
            self.cut_to_line_end()
        elif key == "\x19":  # Ctrl-Y
            self.yank()
        elif key == "\x17":  # Ctrl-W
            self.delete_previous_word()
        elif key == "\x15":  # Ctrl-U
            self.delete_to_line_start()
        elif key == "\x14":  # Ctrl-T
            self.transpose()
        elif key == "\x1b":  # Ctrl-[ is the same byte as Escape.
            self.drag_paragraph(-1)
        elif key == "\x1d":  # Ctrl-]
            self.drag_paragraph(1)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.insert_newline()
        elif key == "\t":
            self.insert("\t")
        elif isinstance(key, str) and key.isprintable():
            self.insert(key)
        elif key == curses.KEY_RESIZE:
            self.stop_coalescing()
        return True

    def run(self, screen: curses.window) -> None:
        curses.raw()
        try:
            curses.set_escdelay(25)
        except AttributeError:
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(50)
        self.draw(screen)

        try:
            while True:
                key: object | None
                try:
                    key = screen.get_wch()
                except curses.error:
                    key = None

                if key is not None:
                    _, screen_width = screen.getmaxyx()
                    if not self.handle_key(key, min(EDIT_WIDTH, screen_width)):
                        return
                    self.draw(screen)
                elif self.autosave_if_due():
                    self.draw(screen)
        finally:
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            curses.noraw()


def read_document(path: Path) -> tuple[str, str, bool]:
    if path.exists():
        if not path.is_file():
            raise OSError(f"not a regular file: {path}")
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise OSError(f"refusing to edit a binary file: {path}")
        text = raw.decode("utf-8")
        newline = "\r\n" if b"\r\n" in raw else "\n"
        return text, newline, False
    if not path.parent.is_dir():
        raise OSError(f"directory does not exist: {path.parent}")
    return "", "\n", True


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="terminaleditor",
        description="Edit one UTF-8 text file in a minimal terminal interface.",
    )
    parser.add_argument("file", type=Path, help="file to create or edit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        path = arguments.file.expanduser().resolve()
        text, newline, is_new = read_document(path)
        editor = Editor(path, text, newline, is_new)
        curses.wrapper(editor.run)
    except (OSError, UnicodeError, curses.error) as error:
        print(f"terminaleditor: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
