from tg_business_bridge.formatting import HTML, to_html, visible_text


def test_visible_text_plain_unchanged():
    assert visible_text("текст с <уголками> и &", None) == "текст с <уголками> и &"


def test_visible_text_strips_tags_and_unescapes():
    src = '<b>жирный</b> и <a href="https://example.com/very/long/path">ссылка</a> &amp; хвост'
    assert visible_text(src, HTML) == "жирный и ссылка & хвост"


def test_visible_text_ignores_href_length():
    src = '<a href="https://example.com/' + "x" * 5000 + '">коротко</a>'
    assert len(visible_text(src, HTML)) == len("коротко")


def test_to_html_escapes_plain_text():
    assert to_html("1 < 2 & 3 > 0", None) == "1 &lt; 2 &amp; 3 &gt; 0"


def test_to_html_keeps_markup_as_is():
    src = '<b>привет</b> <a href="https://ya.ru">тут</a>'
    assert to_html(src, HTML) == src
