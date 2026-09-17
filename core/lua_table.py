"""Minimal Lua-table-literal parser, scoped to exactly what WoW's
SavedVariables serializer emits for our own addon (nested tables, quoted
string/number/bool values, no functions or other Lua expressions).

Exists because slpp (the general-purpose PyPI Lua parser) mangles non-ASCII
bytes inside string literals - confirmed live against a realm name
containing "E with grave" (UTF-8 bytes C3 88): slpp's parser walks the raw
byte stream character-by-character and corrupts any multi-byte UTF-8
sequence it doesn't specifically special-case. Since we fully control the
input grammar (it's our own addon's output, not arbitrary user Lua), a
small parser scoped to that grammar sidesteps the bug entirely instead of
working around a third-party library's internals.
"""
import re

_WS = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def _skip_ws(self):
        m = _WS.match(self.text, self.pos)
        self.pos = m.end()

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _expect(self, ch: str):
        if self._peek() != ch:
            raise ValueError(f"expected {ch!r} at position {self.pos}, got {self.text[self.pos:self.pos+20]!r}")
        self.pos += 1

    def parse_value(self):
        self._skip_ws()
        c = self._peek()
        if c == "{":
            return self.parse_table()
        if c == '"':
            return self.parse_string()
        if self.text.startswith("true", self.pos):
            self.pos += 4
            return True
        if self.text.startswith("false", self.pos):
            self.pos += 5
            return False
        if self.text.startswith("nil", self.pos):
            self.pos += 3
            return None
        m = _NUMBER.match(self.text, self.pos)
        if m:
            self.pos = m.end()
            s = m.group(0)
            return float(s) if "." in s else int(s)
        raise ValueError(f"unexpected value at position {self.pos}: {self.text[self.pos:self.pos+20]!r}")

    def parse_string(self) -> str:
        self._expect('"')
        start = self.pos
        out = []
        while True:
            c = self._peek()
            if c == "":
                raise ValueError("unterminated string")
            if c == '"':
                out.append(self.text[start:self.pos])
                self.pos += 1
                return "".join(out)
            if c == "\\":
                out.append(self.text[start:self.pos])
                nxt = self.text[self.pos + 1] if self.pos + 1 < len(self.text) else ""
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                self.pos += 2
                start = self.pos
                continue
            self.pos += 1

    def parse_table(self):
        self._expect("{")
        obj = {}
        arr = []
        is_array = True
        idx = 1
        while True:
            self._skip_ws()
            if self._peek() == "}":
                self.pos += 1
                break
            if self._peek() == "[":
                self.pos += 1
                self._skip_ws()
                key = self.parse_string() if self._peek() == '"' else self._parse_number_key()
                self._skip_ws()
                self._expect("]")
                self._skip_ws()
                self._expect("=")
                value = self.parse_value()
                obj[key] = value
                is_array = False
            else:
                m = _IDENT.match(self.text, self.pos)
                if m:
                    key = m.group(0)
                    self.pos = m.end()
                    self._skip_ws()
                    self._expect("=")
                    value = self.parse_value()
                    obj[key] = value
                    is_array = False
                else:
                    value = self.parse_value()
                    arr.append(value)
                    obj[idx] = value
                    idx += 1
            self._skip_ws()
            if self._peek() in (",", ";"):
                self.pos += 1
        return arr if is_array else obj

    def _parse_number_key(self):
        m = _NUMBER.match(self.text, self.pos)
        if not m:
            raise ValueError(f"expected numeric table key at position {self.pos}")
        self.pos = m.end()
        s = m.group(0)
        return int(s) if "." not in s else float(s)


def parse(text: str):
    """Parse a Lua table literal (the `{ ... }` part only - strip any
    leading `Name = ` assignment before calling)."""
    p = _Parser(text.strip())
    return p.parse_value()
