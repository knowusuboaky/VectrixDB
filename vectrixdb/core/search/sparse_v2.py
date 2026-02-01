"""
Improved Sparse Vector Search v2

Enhanced BM25 with:
- Porter/Snowball stemming
- Stopword removal
- Subword tokenization
- BM25+ variant for better short document handling
- Query expansion with synonyms
"""

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

# Try to import NLTK for better NLP
try:
    import nltk
    from nltk.stem import PorterStemmer, SnowballStemmer
    from nltk.corpus import stopwords
    NLTK_AVAILABLE = True

    # Download required NLTK data
    try:
        nltk.data.find('corpora/stopwords')
    except LookupError:
        nltk.download('stopwords', quiet=True)
    try:
        nltk.data.find('tokenizers/punkt')
    except LookupError:
        nltk.download('punkt', quiet=True)
except ImportError:
    NLTK_AVAILABLE = False


# English stopwords (fallback if NLTK not available)
ENGLISH_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been',
    'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare', 'ought',
    'used', 'it', 'its', 'this', 'that', 'these', 'those', 'i', 'you', 'he',
    'she', 'we', 'they', 'me', 'him', 'her', 'us', 'them', 'my', 'your',
    'his', 'our', 'their', 'mine', 'yours', 'hers', 'ours', 'theirs', 'what',
    'which', 'who', 'whom', 'whose', 'where', 'when', 'why', 'how', 'all',
    'each', 'every', 'both', 'few', 'more', 'most', 'other', 'some', 'such',
    'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very',
    'just', 'also', 'now', 'here', 'there', 'then', 'once', 'if', 'because',
    'until', 'while', 'about', 'against', 'between', 'into', 'through',
    'during', 'before', 'after', 'above', 'below', 'up', 'down', 'out',
    'off', 'over', 'under', 'again', 'further', 'any', 'being', 'having',
}


class PorterStemmerFallback:
    """Simple Porter Stemmer fallback if NLTK not available."""

    def stem(self, word: str) -> str:
        """Basic stemming rules."""
        word = word.lower()

        # Simple suffix removal
        if word.endswith('ing'):
            if len(word) > 5:
                if word[-4] == word[-5]:  # running -> run
                    return word[:-4]
                return word[:-3]
        elif word.endswith('ed'):
            if len(word) > 4:
                return word[:-2]
        elif word.endswith('ly'):
            if len(word) > 4:
                return word[:-2]
        elif word.endswith('ies'):
            if len(word) > 4:
                return word[:-3] + 'y'
        elif word.endswith('es'):
            if len(word) > 4:
                return word[:-2]
        elif word.endswith('s') and not word.endswith('ss'):
            if len(word) > 3:
                return word[:-1]
        elif word.endswith('tion'):
            if len(word) > 5:
                return word[:-4] + 't'
        elif word.endswith('ment'):
            if len(word) > 5:
                return word[:-4]
        elif word.endswith('ness'):
            if len(word) > 5:
                return word[:-4]
        elif word.endswith('able'):
            if len(word) > 5:
                return word[:-4]
        elif word.endswith('ible'):
            if len(word) > 5:
                return word[:-4]

        return word


@dataclass
class SparseSearchResultV2:
    """Sparse search result."""
    id: str
    score: float
    matched_terms: int = 0
    bm25_score: float = 0.0
    payload: Optional[Dict[str, Any]] = None


class EnhancedTokenizer:
    """
    Enhanced tokenizer with stemming, stopwords, and n-grams.
    """

    def __init__(
        self,
        use_stemming: bool = True,
        remove_stopwords: bool = True,
        min_token_length: int = 2,
        max_token_length: int = 50,
        use_ngrams: bool = False,
        ngram_range: Tuple[int, int] = (2, 3),
    ):
        """
        Initialize tokenizer.

        Args:
            use_stemming: Apply Porter stemming
            remove_stopwords: Remove common stopwords
            min_token_length: Minimum token length
            max_token_length: Maximum token length
            use_ngrams: Generate character n-grams
            ngram_range: N-gram sizes (min, max)
        """
        self.use_stemming = use_stemming
        self.remove_stopwords = remove_stopwords
        self.min_token_length = min_token_length
        self.max_token_length = max_token_length
        self.use_ngrams = use_ngrams
        self.ngram_range = ngram_range

        # Initialize stemmer
        if NLTK_AVAILABLE and use_stemming:
            self.stemmer = SnowballStemmer('english')
        elif use_stemming:
            self.stemmer = PorterStemmerFallback()
        else:
            self.stemmer = None

        # Initialize stopwords
        if NLTK_AVAILABLE and remove_stopwords:
            self.stopwords = set(stopwords.words('english'))
        elif remove_stopwords:
            self.stopwords = ENGLISH_STOPWORDS
        else:
            self.stopwords = set()

    def tokenize(self, text: str) -> List[str]:
        """
        Tokenize text with all enhancements.

        Args:
            text: Input text

        Returns:
            List of processed tokens
        """
        # Lowercase
        text = text.lower()

        # Extract words (including hyphenated and apostrophe words)
        tokens = re.findall(r"\b[a-z][a-z'-]*[a-z]\b|[a-z]+", text)

        # Process tokens
        processed = []
        for token in tokens:
            # Skip stopwords
            if self.remove_stopwords and token in self.stopwords:
                continue

            # Skip by length
            if len(token) < self.min_token_length or len(token) > self.max_token_length:
                continue

            # Apply stemming
            if self.stemmer:
                token = self.stemmer.stem(token)

            # Skip if stemmed to stopword or too short
            if token in self.stopwords or len(token) < self.min_token_length:
                continue

            processed.append(token)

        # Add n-grams if enabled
        if self.use_ngrams:
            ngrams = self._generate_ngrams(text)
            processed.extend(ngrams)

        return processed

    def _generate_ngrams(self, text: str) -> List[str]:
        """Generate character n-grams."""
        text = re.sub(r'[^a-z]', ' ', text.lower())
        words = text.split()

        ngrams = []
        min_n, max_n = self.ngram_range

        for word in words:
            if len(word) < min_n:
                continue
            for n in range(min_n, min(max_n + 1, len(word) + 1)):
                for i in range(len(word) - n + 1):
                    ngrams.append(f"#{word[i:i+n]}#")

        return ngrams


class BM25Plus:
    """
    BM25+ implementation - better handling of term frequency saturation
    and document length normalization.

    BM25+ adds a small delta to IDF to ensure all matching terms contribute
    positively, even in very long documents.

    Reference: Lv & Zhai, "Lower-Bounding Term Frequency Normalization" (CIKM 2011)
    """

    def __init__(
        self,
        k1: float = 1.2,
        b: float = 0.75,
        delta: float = 1.0,
        tokenizer: Optional[EnhancedTokenizer] = None,
    ):
        """
        Initialize BM25+.

        Args:
            k1: Term frequency saturation (1.2-2.0)
            b: Length normalization (0-1, 0.75 typical)
            delta: Lower bound delta for BM25+ (typically 1.0)
            tokenizer: Custom tokenizer (uses EnhancedTokenizer if None)
        """
        self.k1 = k1
        self.b = b
        self.delta = delta
        self.tokenizer = tokenizer or EnhancedTokenizer()

        # Document storage
        self._doc_tokens: Dict[str, List[str]] = {}
        self._doc_tf: Dict[str, Dict[str, int]] = {}
        self._doc_lengths: Dict[str, int] = {}

        # Corpus statistics
        self._df: Dict[str, int] = defaultdict(int)
        self._total_docs = 0
        self._avg_doc_length = 0.0
        self._total_tokens = 0

        # Precomputed IDF cache
        self._idf_cache: Dict[str, float] = {}

    def add_document(self, doc_id: str, text: str) -> None:
        """
        Add a document to the index.

        Args:
            doc_id: Document ID
            text: Document text
        """
        # Remove old version
        if doc_id in self._doc_tf:
            self.remove_document(doc_id)

        # Tokenize
        tokens = self.tokenizer.tokenize(text)

        if not tokens:
            return

        # Compute term frequencies
        tf: Dict[str, int] = defaultdict(int)
        for token in tokens:
            tf[token] += 1

        # Store
        self._doc_tokens[doc_id] = tokens
        self._doc_tf[doc_id] = dict(tf)
        self._doc_lengths[doc_id] = len(tokens)

        # Update document frequencies
        for term in tf:
            self._df[term] += 1

        # Update stats
        self._total_docs += 1
        self._total_tokens += len(tokens)
        self._avg_doc_length = self._total_tokens / self._total_docs

        # Invalidate IDF cache
        self._idf_cache.clear()

    def add_documents(self, documents: List[Tuple[str, str]]) -> int:
        """
        Add multiple documents.

        Args:
            documents: List of (doc_id, text) tuples

        Returns:
            Number of documents added
        """
        for doc_id, text in documents:
            self.add_document(doc_id, text)
        return len(documents)

    def remove_document(self, doc_id: str) -> bool:
        """Remove a document."""
        if doc_id not in self._doc_tf:
            return False

        # Update document frequencies
        for term in self._doc_tf[doc_id]:
            self._df[term] -= 1
            if self._df[term] <= 0:
                del self._df[term]

        # Update stats
        self._total_tokens -= self._doc_lengths[doc_id]
        self._total_docs -= 1

        if self._total_docs > 0:
            self._avg_doc_length = self._total_tokens / self._total_docs
        else:
            self._avg_doc_length = 0.0

        # Remove document
        del self._doc_tokens[doc_id]
        del self._doc_tf[doc_id]
        del self._doc_lengths[doc_id]

        # Invalidate cache
        self._idf_cache.clear()

        return True

    def _compute_idf(self, term: str) -> float:
        """Compute IDF for a term."""
        if term in self._idf_cache:
            return self._idf_cache[term]

        df = self._df.get(term, 0)
        if df == 0:
            return 0.0

        # BM25+ IDF: log((N + 1) / (df + 0.5))
        idf = math.log((self._total_docs + 1) / (df + 0.5))
        self._idf_cache[term] = idf

        return idf

    def score(
        self,
        query: str,
        k: int = 10,
        filter_ids: Optional[Set[str]] = None,
    ) -> List[SparseSearchResultV2]:
        """
        Score documents for a query.

        Args:
            query: Query text
            k: Number of results
            filter_ids: Only score these documents

        Returns:
            Scored results
        """
        # Tokenize query
        query_tokens = self.tokenizer.tokenize(query)

        if not query_tokens:
            return []

        # Get unique query terms with frequencies
        query_tf: Dict[str, int] = defaultdict(int)
        for token in query_tokens:
            query_tf[token] += 1

        # Compute query term IDFs
        query_idfs = {term: self._compute_idf(term) for term in query_tf}

        # Score documents
        scores: Dict[str, float] = defaultdict(float)
        matched: Dict[str, int] = defaultdict(int)

        # Only consider documents that contain at least one query term
        candidate_docs = set()
        for term in query_tf:
            if term in self._df:
                for doc_id in self._doc_tf:
                    if term in self._doc_tf[doc_id]:
                        candidate_docs.add(doc_id)

        # Apply filter
        if filter_ids:
            candidate_docs &= filter_ids

        for doc_id in candidate_docs:
            doc_tf = self._doc_tf[doc_id]
            doc_len = self._doc_lengths[doc_id]

            # Length normalization factor
            len_norm = 1 - self.b + self.b * (doc_len / self._avg_doc_length)

            for term, query_freq in query_tf.items():
                if term not in doc_tf:
                    continue

                tf = doc_tf[term]
                idf = query_idfs[term]

                if idf <= 0:
                    continue

                # BM25+ formula
                # score = IDF * (tf * (k1 + 1)) / (tf + k1 * len_norm) + delta
                tf_component = (tf * (self.k1 + 1)) / (tf + self.k1 * len_norm)

                # Add delta for BM25+ (ensures positive contribution)
                term_score = idf * (tf_component + self.delta)

                # Weight by query term frequency
                scores[doc_id] += term_score * query_freq
                matched[doc_id] += 1

        # Sort by score
        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # Return top k
        results = []
        for doc_id, score in sorted_docs[:k]:
            results.append(SparseSearchResultV2(
                id=doc_id,
                score=score,
                matched_terms=matched[doc_id],
                bm25_score=score,
            ))

        return results

    def get_scores(self, query: str) -> Dict[str, float]:
        """
        Get all document scores for a query (for fusion).

        Args:
            query: Query text

        Returns:
            Dict of {doc_id: score}
        """
        results = self.score(query, k=len(self._doc_tf))
        return {r.id: r.score for r in results}

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        return {
            "total_documents": self._total_docs,
            "vocabulary_size": len(self._df),
            "avg_document_length": self._avg_doc_length,
            "total_tokens": self._total_tokens,
            "k1": self.k1,
            "b": self.b,
            "delta": self.delta,
        }


# Convenience function
def create_bm25_plus(
    k1: float = 1.2,
    b: float = 0.75,
    delta: float = 1.0,
    use_stemming: bool = True,
    remove_stopwords: bool = True,
) -> BM25Plus:
    """
    Create a BM25+ index with custom settings.

    Args:
        k1: Term frequency saturation
        b: Length normalization
        delta: BM25+ delta
        use_stemming: Enable stemming
        remove_stopwords: Enable stopword removal

    Returns:
        Configured BM25+ index
    """
    tokenizer = EnhancedTokenizer(
        use_stemming=use_stemming,
        remove_stopwords=remove_stopwords,
    )
    return BM25Plus(k1=k1, b=b, delta=delta, tokenizer=tokenizer)
