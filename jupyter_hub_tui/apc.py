# APC parser for kitty graphics protocol.
# Buffers incomplete APC sequences across PTY reads.
# Returns clean text for pyte and complete APC sequences for forwarding.

from __future__ import annotations

APC_START = "\x1b_G"
APC_END = "\x1b\\"


class APCStream:
    def __init__(self) -> None:
        self._buf = ""

    def feed(self, data: str) -> tuple[str, list[str]]:
        # Feed raw text. Returns (clean_for_pyte, apc_sequences).
        self._buf += data
        clean: list[str] = []
        apcs: list[str] = []

        while True:
            si = self._buf.find(APC_START)
            if si == -1:
                # No APC. Flush all but potential partial start.
                if self._buf.endswith("\x1b"):
                    clean.append(self._buf[:-1])
                    self._buf = "\x1b"
                else:
                    clean.append(self._buf)
                    self._buf = ""
                break

            # Emit clean text before APC.
            if si > 0:
                clean.append(self._buf[:si])

            ei = self._buf.find(APC_END, si + len(APC_START))
            if ei == -1:
                # Incomplete APC. Keep from si onward.
                self._buf = self._buf[si:]
                break

            # Complete APC.
            apc = self._buf[si : ei + len(APC_END)]
            apcs.append(apc)
            self._buf = self._buf[ei + len(APC_END) :]

        return ("".join(clean), apcs)

    def flush(self) -> str:
        # Return any remaining buffered text (for shutdown).
        text = self._buf
        self._buf = ""
        return text


if __name__ == "__main__":
    # Self-check: complete APC in one read.
    s = APCStream()
    clean, apcs = s.feed("hello\x1b_Ga=t,i=1\x1b\\world")
    assert clean == "helloworld", f"got {clean!r}"
    assert len(apcs) == 1 and "a=t" in apcs[0], f"got {apcs!r}"
    print("test1 pass")

    # Self-check: APC split across reads.
    s = APCStream()
    clean1, apcs1 = s.feed("hello\x1b_Ga=t,i=1")
    assert clean1 == "hello", f"got {clean1!r}"
    assert apcs1 == [], f"got {apcs1!r}"
    clean2, apcs2 = s.feed(",f=100\x1b\\world")
    assert clean2 == "world", f"got {clean2!r}"
    assert len(apcs2) == 1 and "f=100" in apcs2[0], f"got {apcs2!r}"
    print("test2 pass")

    # Self-check: multiple APCs in one read.
    s = APCStream()
    clean, apcs = s.feed("a\x1b_Gx\x1b\\b\x1b_Gy\x1b\\c")
    assert clean == "abc", f"got {clean!r}"
    assert len(apcs) == 2, f"got {apcs!r}"
    print("test3 pass")

    # Self-check: lone ESC at end.
    s = APCStream()
    clean, apcs = s.feed("abc\x1b")
    assert clean == "abc", f"got {clean!r}"
    assert apcs == [], f"got {apcs!r}"
    clean2, _ = s.feed("_Gtest\x1b\\")
    assert clean2 == "", f"got {clean2!r}"
    print("test4 pass")

    print("ALL PASS")
