"""
Serviço de NLP para cálculo de similaridade de texto entre produtos.

Design:
- Vocabulário carregado de data/nlp_vocabulary.json (zero hardcode no código)
- Pesos e thresholds de penalidade injetados via RelevanceSettings (config.py)
- Interface única: calculate_text_score(official_title, marketplace_title) → float [0.0, 1.0]
"""

import json
import logging
import os
import re
import unicodedata
import html
from functools import cached_property
from typing import FrozenSet, Dict, Set

from rapidfuzz import fuzz, utils

from config import relevance_settings

logger = logging.getLogger(__name__)


class NLPVocabulary:
    """
    Vocabulário NLP carregado a partir de um arquivo JSON externo.

    Responsabilidade única: fornecer os conjuntos de palavras utilizados
    pelo pipeline de scoring. Nenhuma lógica de negócio aqui.
    """

    def __init__(self, vocabulary_path: str) -> None:
        self._path = vocabulary_path
        self._data = self._load(vocabulary_path)

    @staticmethod
    def _load(path: str) -> dict:
        """Carrega o JSON de vocabulário a partir de um caminho relativo à raiz do projeto."""
        # Resolve o caminho em relação ao diretório raiz do projeto (onde config.py vive)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        abs_path = os.path.join(root, path)
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(
                f"Arquivo de vocabulário NLP não encontrado: '{abs_path}'. "
                "Usando vocabulário vazio — resultados de scoring podem ser imprecisos."
            )
            return {}
        except json.JSONDecodeError as exc:
            logger.error(
                f"Vocabulário NLP inválido (JSON malformado) em '{abs_path}': {exc}. "
                "Usando vocabulário vazio."
            )
            return {}

    @cached_property
    def colors(self) -> FrozenSet[str]:
        return frozenset(self._data.get("colors", []))

    @cached_property
    def brand_names(self) -> FrozenSet[str]:
        return frozenset(self._data.get("brand_names", []))

    @cached_property
    def category_words(self) -> FrozenSet[str]:
        return frozenset(self._data.get("category_words", []))

    @cached_property
    def stop_words(self) -> FrozenSet[str]:
        return frozenset(self._data.get("stop_words", []))

    @cached_property
    def noise_words(self) -> FrozenSet[str]:
        return frozenset(self._data.get("noise_words", []))

    @cached_property
    def category_synonyms(self) -> Dict[str, Set[str]]:
        return {k: set(v) for k, v in self._data.get("category_synonyms", {}).items()}

    @cached_property
    def known_brands_for_detection(self) -> FrozenSet[str]:
        return frozenset(self._data.get("known_brands_for_brand_detection", []))

    @cached_property
    def brand_and_category_words(self) -> FrozenSet[str]:
        """União de marcas + categorias + cores — palavras que NÃO identificam modelo."""
        return self.brand_names | self.category_words | self.colors


class NLPService:
    """
    Calcula similaridade semântica entre títulos de produtos cross-marketplace.

    Estratégia:
    1. Remove cores de ambos os títulos (variações de cor não indicam modelo diferente)
    2. Blend ponderado de 3 métricas fuzzy (WRatio + token_sort + partial_token_set)
    3. Penalidade de model-words: suavizada quando a marca está presente
    4. Penalidade de categoria: evita cruzamento de categorias incompatíveis

    Todos os pesos e thresholds são lidos de RelevanceSettings → configuráveis via .env.
    """

    def __init__(self, vocabulary: NLPVocabulary) -> None:
        self._vocab = vocabulary
        self._cfg = relevance_settings

    # ------------------------------------------------------------------
    # Helpers de normalização
    # ------------------------------------------------------------------

    def _normalize(self, text: str) -> str:
        """Remove acentos e converte para ASCII lowercase."""
        return (
            unicodedata.normalize("NFD", text)
            .encode("ascii", "ignore")
            .decode("utf-8")
            .lower()
        )

    def _clean_text(self, text: str) -> str:
        """
        Pipeline de limpeza de texto para comparação fuzzy.

        Ordem de operações:
          1. Unescape de entidades HTML
          2. Remove separadores SEO e marcas registradas
          3. Remove acentos → ASCII
          4. Normalização Rapidfuzz (lowercase + squash whitespace)
          5. Remove noise words (anúncio, patrocinado, etc.)
        """
        if not text:
            return ""

        text = html.unescape(text)
        text = re.sub(r"[®™|\-]", " ", text)
        text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("utf-8")
        text = utils.default_process(text)

        for word in self._vocab.noise_words:
            text = re.sub(rf"\b{re.escape(word)}\b", "", text)

        text = re.sub(r"\s+", " ", text).strip()
        return text

    def remove_colors(self, text: str) -> str:
        """Remove nomes de cores do texto para ampliar a busca."""
        if not text:
            return ""
        words = text.split()
        return " ".join(
            w for w in words
            if self._normalize(w) not in self._vocab.colors
        )

    # ------------------------------------------------------------------
    # Score principal
    # ------------------------------------------------------------------

    def calculate_text_score(
        self,
        official_title: str,
        marketplace_title: str,
    ) -> float:
        """
        Calcula a similaridade entre o título oficial e o título do marketplace.
        Retorna um float entre 0.0 e 1.0.
        """
        if not official_title or not marketplace_title:
            return 0.0

        # 1. Remove cores de ambos os lados — cor é atributo de variante, não de modelo
        official_no_color = self.remove_colors(official_title)
        market_no_color = self.remove_colors(marketplace_title)

        clean_official = self._clean_text(official_no_color)
        clean_market = self._clean_text(market_no_color)

        if not clean_official or not clean_market:
            return 0.0

        # 2. Blend ponderado de métricas fuzzy
        wratio = fuzz.WRatio(clean_official, clean_market) / 100.0
        tsort = fuzz.token_sort_ratio(clean_official, clean_market) / 100.0
        ptset = fuzz.partial_token_set_ratio(clean_official, clean_market) / 100.0

        cfg = self._cfg
        score = (
            wratio * cfg.NLP_WRATIO_WEIGHT
            + tsort * cfg.NLP_TOKEN_SORT_WEIGHT
            + ptset * cfg.NLP_PARTIAL_SET_WEIGHT
        )

        # 3. Penalidade de model-words
        score = self._apply_model_word_penalty(score, clean_official, clean_market)

        # 4. Penalidade de categoria
        score = self._apply_category_penalty(score, clean_official, clean_market)

        # 5. Penalidade de marca ausente/divergente
        score = self._apply_brand_penalty(score, clean_official, clean_market)

        return max(0.0, min(1.0, score))

    # ------------------------------------------------------------------
    # Penalidades
    # ------------------------------------------------------------------

    def _apply_model_word_penalty(
        self,
        score: float,
        clean_official: str,
        clean_market: str,
    ) -> float:
        """
        Penaliza quando o produto do marketplace não contém as palavras
        que identificam o modelo específico (além de marca/categoria/cor).

        Suaviza a penalidade quando a marca do produto original está presente
        no título do marketplace — indica ao menos a mesma linha de fabricante.
        """
        vocab = self._vocab
        cfg = self._cfg

        official_words = clean_official.split()
        market_words = set(clean_market.split())

        # model_words = palavras que identificam modelo (≥ 3 chars, não são marca/categoria/stopword)
        model_words = [
            w for w in official_words
            if (
                len(w) >= 3
                and w not in vocab.brand_and_category_words
                and w not in vocab.stop_words
            )
        ]

        if not model_words:
            return score

        model_hits = sum(1 for w in model_words if w in market_words)
        model_ratio = model_hits / len(model_words)

        brand_present = any(
            b in market_words for b in vocab.known_brands_for_detection
        )

        if model_ratio < cfg.NLP_MODEL_PENALTY_LOW_THRESHOLD:
            multiplier = (
                cfg.NLP_MODEL_PENALTY_HEAVY_WITH_BRAND
                if brand_present
                else cfg.NLP_MODEL_PENALTY_HEAVY_WITHOUT_BRAND
            )
            score *= multiplier
        elif model_ratio < cfg.NLP_MODEL_PENALTY_MED_THRESHOLD:
            multiplier = (
                cfg.NLP_MODEL_PENALTY_MED_WITH_BRAND
                if brand_present
                else cfg.NLP_MODEL_PENALTY_MED_WITHOUT_BRAND
            )
            score *= multiplier

        return score

    def _apply_category_penalty(
        self,
        score: float,
        clean_official: str,
        clean_market: str,
    ) -> float:
        """
        Penaliza quando a categoria principal do produto oficial não bate
        com nenhum sinônimo válido no título do marketplace.

        Ex: "Tênis" vs "Camisa" → incompatível → penalidade.
        Os sinônimos são carregados do vocabulário externo (category_synonyms).
        """
        cfg = self._cfg
        official_words = clean_official.split()
        market_words = set(clean_market.split())
        synonyms = self._vocab.category_synonyms

        if not official_words:
            return score

        # Detecta substantivo-cabeça (pode ser bigrama, ex: "t shirt")
        head_noun = official_words[0]
        if (
            len(official_words) > 1
            and f"{official_words[0]} {official_words[1]}" in synonyms
        ):
            head_noun = f"{official_words[0]} {official_words[1]}"

        valid_nouns = synonyms.get(head_noun, {head_noun})

        if not any(noun in market_words for noun in valid_nouns):
            if score > cfg.NLP_CATEGORY_HIGH_SCORE_THRESHOLD:
                score *= cfg.NLP_CATEGORY_PENALTY_HIGH_SCORE
            else:
                score *= cfg.NLP_CATEGORY_PENALTY_LOW_SCORE

        return score

    def brand_is_present(self, official_title: str, marketplace_title: str) -> bool:
        """
        Retorna True se a query NÃO especifica marca conhecida (no-op → mantém item),
        ou se o título do marketplace contém ao menos uma das marcas da query.
        Retorna False apenas quando a query especifica marca conhecida e o título do
        marketplace não a contém (item a descartar).

        Normalização: aplica apenas ``_clean_text`` em ambos os lados (sem
        ``remove_colors``). Isso é intencional e correto: as marcas em
        ``known_brands_for_detection`` (aramis, reserva, tommy) nunca são tokens de
        cor — ``known_brands_for_detection ∩ colors == ∅`` — portanto remover cores
        jamais removeria uma marca; o passo ``remove_colors`` é supérfluo para a
        decisão de marca e é omitido de propósito por essa razão. O veredito de marca
        é equivalente para o vocabulário corrente, por construção (marcas ∉ colors).

        Comportamento em edge cases:
        - Título do marketplace vazio/None com marca conhecida na query → False
          (fail-closed: ``_clean_text("")`` retorna ``""``, ``market_words`` fica
          vazio, ``any(...)`` retorna False naturalmente).
        - Official vazio/None → sem marca detectável → retorna True (no-op fail-open;
          não há critério de marca a aplicar). Não levanta exceção.

        Args:
            official_title: Título oficial bruto (raw) do produto buscado.
            marketplace_title: Título bruto (raw) do produto do marketplace.

        Returns:
            bool — True para manter o item, False para descartá-lo.
        """
        clean_official = self._clean_text(official_title)
        clean_market = self._clean_text(marketplace_title)
        official_words = set(clean_official.split())
        market_words = set(clean_market.split())

        brands_in_query = official_words.intersection(self._vocab.known_brands_for_detection)
        if not brands_in_query:
            return True  # no-op: query sem marca conhecida não filtra nada
        return any(b in market_words for b in brands_in_query)

    def _apply_brand_penalty(self, score: float, clean_official: str, clean_market: str) -> float:
        """
        Penaliza severamente se a query original especifica uma marca conhecida,
        mas o título do marketplace não contém essa marca.
        Evita que "Polo Aramis" retorne "Polo Hering" com score alto.
        """
        official_words = set(clean_official.split())
        market_words = set(clean_market.split())
        
        # Quais marcas estão presentes na query original?
        brands_in_query = official_words.intersection(self._vocab.known_brands_for_detection)
        
        if not brands_in_query:
            return score # A query não especifica marca, não penaliza.
            
        # Pelo menos uma das marcas da query deve estar no título do marketplace
        if any(b in market_words for b in brands_in_query):
            return score # Marca encontrada!
            
        # Marca ausente no título do marketplace! Penalidade pesada.
        # Ex: Busca "aramis", mas marketplace retornou sem "aramis"
        return score * 0.50 # Reduz pela metade



# ---------------------------------------------------------------------------
# Singleton — instanciado uma vez na inicialização da aplicação.
# O vocabulário é carregado do JSON apenas neste momento.
# ---------------------------------------------------------------------------
_vocabulary = NLPVocabulary(vocabulary_path=relevance_settings.NLP_VOCABULARY_PATH)
nlp_service = NLPService(vocabulary=_vocabulary)
