from __future__ import annotations

from pipelines.extract import html_to_text

HTML = """
<html><head><style>.x{color:red}</style><script>alert(1)</script></head>
<body>
<div>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</div>
<p>Item 7. Management's Discussion and Analysis</p>
<table>
  <tr><td>Revenue</td><td>100</td></tr>
  <tr><td>Cost</td><td>60</td></tr>
</table>
<p>Net income was <b>40</b> million.</p>
</body></html>
"""


def test_strips_script_and_style() -> None:
    text = html_to_text(HTML)
    assert "alert(1)" not in text
    assert "color:red" not in text


def test_keeps_block_structure() -> None:
    text = html_to_text(HTML)
    lines = text.splitlines()
    assert lines[0] == "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"
    assert any(line.startswith("Item 7.") for line in lines)


def test_inline_text_not_broken() -> None:
    text = html_to_text(HTML)
    assert "Net income was 40 million." in text.replace("\n", " ")
