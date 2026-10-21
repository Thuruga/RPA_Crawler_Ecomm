from services.sanitization import sanitize_colors, sanitize_sizes

def test_sanitize_colors():
    assert sanitize_colors(["azul", " verde "]) == ["Azul", "Verde"]
    assert sanitize_colors(["azul marinho", " BRANCO "]) == ["Azul Marinho", "Branco"]
    assert sanitize_colors(["verde/p", "preto - m"]) == ["Verde/p", "Preto - M"]
    assert sanitize_colors(["azul", "Azul"]) == ["Azul"]

def test_sanitize_sizes():
    assert sanitize_sizes([" p ", "M "]) == ["P", "M"]
    assert sanitize_sizes(["M", "P", "G"]) == ["P", "M", "G"]
    assert sanitize_sizes(["pequeno", "MÉDIO", " grande", "unico", "XG"]) == ["P", "M", "G", "GG", "U"]
    assert sanitize_sizes(["38 ", "36"]) == ["36", "38"]
    assert sanitize_sizes(["P", "p", " P "]) == ["P"]
    assert sanitize_sizes(["GG", "P", "M", "XXG"]) == ["P", "M", "GG", "XXG"]
