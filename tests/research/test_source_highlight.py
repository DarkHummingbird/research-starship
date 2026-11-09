from tests.research.test_p2_analysis import paper


def test_highlight_exact_quote_and_version(client, paper, auth):
    _, headers, _, doc = paper
    source = doc['sources'][0]
    body = {'expected_version': doc['version'], 'chunk_id': source['id'], 'quote': 'Baseline: KeywordSearch.'}
    url = f"/api/research/documents/{doc['id']}/highlight"
    result = client.post(url, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.headers['content-type'] == 'image/png'
    assert result.headers['x-source-page'] == '1'
    assert result.content.startswith(b'\x89PNG\r\n\x1a\n')
    import fitz
    image = fitz.Pixmap(result.content)
    pixels = image.samples
    assert any(pixels[i] > 220 and pixels[i + 1] > 200 and pixels[i + 2] < 170 for i in range(0, len(pixels), image.n))
    assert client.post(url, json=body, headers=auth('other-highlight-user')).status_code == 404
    wrong_chunk = {**body, 'chunk_id': 'not-this-document'}
    assert client.post(url, json=wrong_chunk, headers=headers).status_code == 422
    body['quote'] = 'invented source'
    assert client.post(url, json=body, headers=headers).status_code == 422
    body['expected_version'] += 1
    assert client.post(url, json=body, headers=headers).status_code == 409
    assert client.post(url, json=body).status_code in (401, 403, 404)


def test_multiline_pdf_highlight_and_ambiguity():
    import fitz
    from app.research.highlight import page_rectangles

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((40, 40), "First line of the finding\ncontinues on the second line.")
    quote = "First line of the finding\ncontinues on the second line."
    rectangles = page_rectangles(page, quote, {"extraction": "pdf_text"})
    assert len(rectangles) == 2
    page.insert_text((40, 100), quote)
    assert page_rectangles(page, quote, {"extraction": "pdf_text"}) == []
    pdf.close()


def test_ocr_highlight_rejects_ambiguous_and_missing_coordinates():
    import fitz
    from app.research.highlight import page_rectangles

    pdf = fitz.open()
    page = pdf.new_page()
    lines = [{"text": "first line", "box": [10, 10, 80, 25]}, {"text": "second line", "box": [10, 30, 90, 45]}]
    locator = {"extraction": "offline_ocr", "ocr_lines": lines}
    assert len(page_rectangles(page, "first line\nsecond line", locator)) == 2
    assert page_rectangles(page, "first line", {"extraction": "offline_ocr"}) == []
    assert page_rectangles(page, "first line", {"extraction": "offline_ocr", "ocr_lines": lines + lines}) == []
    pdf.close()


def test_multicolumn_glyph_boxes_and_pdf_spacing_are_safe():
    import fitz
    from app.research.highlight import page_rectangles

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((40, 40), "The retrieval model performs well.")
    page.insert_text((330, 40), "Independent right column text.")
    # Pypdf sometimes emits extra spaces even though PDF glyphs are adjacent.
    result = page_rectangles(page, "The retrie val model performs well.", {"extraction": "pdf_text"})
    assert len(result) == 1
    assert result[0].x1 < 300
    assert not page_rectangles(page, "The retrieval model fabricates data.", {"extraction": "pdf_text"})
    pdf.close()
